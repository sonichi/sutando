#!/usr/bin/env python3
"""Tests for src/archive-stale-results.py.

The archiver prevents DM floods (post-mortem 2026-04-15) by moving stale
results/*.txt files to a date-stamped subdirectory before services start.

Key invariants:
  - Only .txt files directly under results/ are archived.
  - Files younger than RETENTION_HOURS are not touched.
  - Files inside archive-* subdirs are never re-archived.
  - DRY_RUN prints without moving.
  - DRY_RUN accepts "0", "false", "no" (case-insensitive) as falsy.
  - Missing results/ dir is a silent no-op (exit 0).
  - Archived files land under results/archive-YYYY-MM-DD/.
  - RETENTION_HOURS env var overrides the default 24h window.

Run: python3 tests/archive-stale-results.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Module loader: must patch env before import because the module reads
# RETENTION_HOURS and DRY_RUN at module level.
# ---------------------------------------------------------------------------

def _load_archiver(retention_hours: int = 24, dry_run: str = "") -> object:
    """Load the archiver module with specified env overrides."""
    saved_rh = os.environ.get("RETENTION_HOURS")
    saved_dr = os.environ.get("DRY_RUN")
    os.environ["RETENTION_HOURS"] = str(retention_hours)
    if dry_run:
        os.environ["DRY_RUN"] = dry_run
    elif "DRY_RUN" in os.environ:
        del os.environ["DRY_RUN"]

    spec = importlib.util.spec_from_file_location(
        f"archive_stale_{retention_hours}_{dry_run}",
        REPO / "src" / "archive-stale-results.py",
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved_rh is not None:
            os.environ["RETENTION_HOURS"] = saved_rh
        elif "RETENTION_HOURS" in os.environ:
            del os.environ["RETENTION_HOURS"]
        if saved_dr is not None:
            os.environ["DRY_RUN"] = saved_dr
        elif "DRY_RUN" in os.environ:
            del os.environ["DRY_RUN"]
    return mod


def _run_main(results_dir: Path, retention_hours: int = 24, dry_run: str = "") -> int:
    """Run archiver.main() against a temp results dir."""
    mod = _load_archiver(retention_hours, dry_run)
    saved = mod.RESULTS
    saved_drrun = mod.DRY_RUN
    mod.RESULTS = results_dir
    if dry_run:
        mod.DRY_RUN = dry_run.strip().lower() not in ("", "0", "false", "no")
    else:
        mod.DRY_RUN = False
    try:
        return mod.main()
    finally:
        mod.RESULTS = saved
        mod.DRY_RUN = saved_drrun


def _make_stale(path: Path, age_hours: float = 30.0) -> None:
    """Create a file and backdated its mtime by age_hours."""
    path.touch()
    mtime = time.time() - age_hours * 3600
    os.utime(path, (mtime, mtime))


def _make_fresh(path: Path, age_hours: float = 0.5) -> None:
    """Create a file fresh enough to NOT be archived by default (< 24h)."""
    path.touch()
    mtime = time.time() - age_hours * 3600
    os.utime(path, (mtime, mtime))


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_missing_results_dir_noop() -> list[str]:
    """When results/ doesn't exist, main() returns 0 (no-op)."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        nonexistent = Path(td) / "results"
        rc = _run_main(nonexistent)
        check("exit code should be 0", rc == 0, fails)
        check("no archive dir created", not any(Path(td).rglob("archive-*")), fails)
    return fails


def test_stale_txt_archived() -> list[str]:
    """Stale .txt files (> RETENTION_HOURS old) are moved to archive dir."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_stale(results / "task-old.txt", age_hours=30)
        _make_stale(results / "proactive-123.txt", age_hours=48)
        rc = _run_main(results, retention_hours=24)
        check("exit code 0", rc == 0, fails)
        # Files should be gone from results/
        check("task-old.txt still in results/", not (results / "task-old.txt").exists(), fails)
        check("proactive-123.txt still in results/", not (results / "proactive-123.txt").exists(), fails)
        # Should be under archive-*
        archives = list(results.glob("archive-*/"))
        check("no archive dir created", len(archives) == 1, fails)
        if archives:
            archived = list(archives[0].glob("*.txt"))
            check(f"expected 2 archived, got {len(archived)}", len(archived) == 2, fails)
    return fails


def test_fresh_files_not_archived() -> list[str]:
    """Files younger than RETENTION_HOURS are not touched."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_fresh(results / "task-new.txt", age_hours=1)
        _make_fresh(results / "result-recent.txt", age_hours=2)
        rc = _run_main(results, retention_hours=24)
        check("exit 0", rc == 0, fails)
        check("fresh file 1 was archived", (results / "task-new.txt").exists(), fails)
        check("fresh file 2 was archived", (results / "result-recent.txt").exists(), fails)
        check("archive dir created unnecessarily", not any(results.glob("archive-*/")), fails)
    return fails


def test_non_txt_files_skipped() -> list[str]:
    """Non-.txt files are never archived, even if stale."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_stale(results / "task-old.txt", 30)   # should be archived
        _make_stale(results / "image.png", 30)       # should NOT be archived
        _make_stale(results / "data.json", 30)       # should NOT be archived
        _make_stale(results / "no-ext", 30)          # should NOT be archived
        _run_main(results, retention_hours=24)
        check("png was archived", (results / "image.png").exists(), fails)
        check("json was archived", (results / "data.json").exists(), fails)
        check("no-ext was archived", (results / "no-ext").exists(), fails)
        check("stale .txt still in results/", not (results / "task-old.txt").exists(), fails)
    return fails


def test_archive_subdirs_not_re_archived() -> list[str]:
    """Files inside existing archive-* subdirectories are never touched."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        # Pre-existing archive dir with old files
        old_archive = results / "archive-2026-01-01"
        old_archive.mkdir()
        _make_stale(old_archive / "already-archived.txt", age_hours=9999)
        rc = _run_main(results, retention_hours=24)
        check("exit 0", rc == 0, fails)
        check("already-archived.txt was moved", (old_archive / "already-archived.txt").exists(), fails)
    return fails


def test_dry_run_does_not_move() -> list[str]:
    """DRY_RUN=1 prints what would be archived but doesn't move files."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_stale(results / "stale.txt", 30)
        rc = _run_main(results, retention_hours=24, dry_run="1")
        check("exit 0 in dry run", rc == 0, fails)
        check("dry-run moved the file", (results / "stale.txt").exists(), fails)
        check("archive dir created in dry run", not any(results.glob("archive-*/")), fails)
    return fails


def test_dry_run_falsy_values() -> list[str]:
    """DRY_RUN='0', 'false', 'no', 'FALSE' (case-insensitive) are NOT dry-run."""
    fails: list[str] = []
    for val in ("0", "false", "no", "FALSE", "No", "False"):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            _make_stale(results / "stale.txt", 30)
            _run_main(results, retention_hours=24, dry_run=val)
            # With a falsy DRY_RUN, file SHOULD be moved (not dry-run)
            check(
                f"DRY_RUN={val!r} treated as dry-run (file not moved)",
                not (results / "stale.txt").exists(),
                fails,
            )
    return fails


def test_retention_hours_override() -> list[str]:
    """RETENTION_HOURS=1 archives files older than 1 hour, leaves newer ones."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_stale(results / "old-2h.txt", age_hours=2)    # > 1h → archive
        _make_fresh(results / "fresh-30m.txt", age_hours=0.5)  # < 1h → keep
        _run_main(results, retention_hours=1)
        check("2h-old not archived", not (results / "old-2h.txt").exists(), fails)
        check("30min-old wrongly archived", (results / "fresh-30m.txt").exists(), fails)
    return fails


def test_archive_dir_created_on_demand() -> list[str]:
    """The archive-YYYY-MM-DD/ directory is created when first file is archived."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_stale(results / "stale.txt", 30)
        # No pre-existing archive dir
        check("archive dir pre-exists", not any(results.glob("archive-*/")), fails)
        _run_main(results, retention_hours=24)
        archives = list(results.glob("archive-*/"))
        check("archive dir not created", len(archives) == 1, fails)
    return fails


def test_mixed_fresh_and_stale() -> list[str]:
    """Only stale files are archived; fresh files co-exist untouched."""
    fails: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        results = Path(td) / "results"
        results.mkdir()
        _make_stale(results / "stale1.txt", 30)
        _make_stale(results / "stale2.txt", 50)
        _make_fresh(results / "fresh1.txt", 1)
        _make_fresh(results / "fresh2.txt", 12)
        _run_main(results, retention_hours=24)
        check("stale1 still in results/", not (results / "stale1.txt").exists(), fails)
        check("stale2 still in results/", not (results / "stale2.txt").exists(), fails)
        check("fresh1 was archived", (results / "fresh1.txt").exists(), fails)
        check("fresh2 was archived", (results / "fresh2.txt").exists(), fails)
        archives = list(results.glob("archive-*/"))
        if archives:
            archived = list(archives[0].glob("*.txt"))
            check(f"expected 2 in archive, got {len(archived)}", len(archived) == 2, fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("missing results/ dir is no-op", test_missing_results_dir_noop),
        ("stale .txt files archived", test_stale_txt_archived),
        ("fresh files not archived", test_fresh_files_not_archived),
        ("non-.txt files skipped", test_non_txt_files_skipped),
        ("archive subdir not re-archived", test_archive_subdirs_not_re_archived),
        ("DRY_RUN=1 does not move", test_dry_run_does_not_move),
        ("DRY_RUN falsy values (0/false/no)", test_dry_run_falsy_values),
        ("RETENTION_HOURS override", test_retention_hours_override),
        ("archive dir created on demand", test_archive_dir_created_on_demand),
        ("mixed fresh and stale", test_mixed_fresh_and_stale),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\narchive-stale-results: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
