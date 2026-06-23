#!/usr/bin/env python3
"""Regression test for _default_memory_dir() slug generation.

Bug: health-check used str(repo).replace("/", "-") which preserved underscores
in path components (e.g. /Users/foo_bar/src/x → -Users-foo_bar-src-x).
Claude Code uses re.sub(r"[^A-Za-z0-9]+", "-", ...) so underscores become
hyphens too (-Users-foo-bar-src-x). On any install where the username or path
contains underscores the health-check pointed at a nonexistent directory and
permanently showed "memory-dir: not yet created".
"""
from __future__ import annotations
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _slug_new(repo_path: str) -> str:
    """Current implementation: all non-alphanumeric → '-'."""
    return re.sub(r"[^A-Za-z0-9]+", "-", repo_path)


def _slug_legacy(repo_path: str) -> str:
    """Old (buggy) implementation: only '/' → '-'."""
    return repo_path.replace("/", "-")


def _resolve_memory_dir(repo_path: str, claude_home: str) -> str:
    """Mirror of health-check._default_memory_dir() logic, injectable for tests."""
    new_slug = _slug_new(repo_path)
    candidate = Path(claude_home) / "projects" / new_slug / "memory"
    if candidate.exists():
        return str(candidate)
    legacy_slug = _slug_legacy(repo_path)
    legacy = Path(claude_home) / "projects" / legacy_slug / "memory"
    if legacy.exists():
        return str(legacy)
    return str(candidate)


class SlugGenerationTests(unittest.TestCase):

    def test_plain_path_no_underscores(self) -> None:
        self.assertEqual(_slug_new("/Users/alice/src/sutando"), "-Users-alice-src-sutando")

    def test_underscore_in_username(self) -> None:
        """Core regression: underscore must become hyphen."""
        slug = _slug_new("/Users/xingyu_xiang/src/sutando")
        self.assertEqual(slug, "-Users-xingyu-xiang-src-sutando")
        self.assertNotIn("_", slug)

    def test_legacy_slug_preserves_underscore(self) -> None:
        """Confirm the old slug was wrong for underscore paths."""
        slug = _slug_legacy("/Users/xingyu_xiang/src/sutando")
        self.assertIn("_", slug, "legacy slug kept underscore — that was the bug")

    def test_new_vs_legacy_diverge_on_underscore(self) -> None:
        repo = "/Users/foo_bar/src/project"
        self.assertNotEqual(_slug_new(repo), _slug_legacy(repo))

    def test_new_vs_legacy_agree_when_no_underscore(self) -> None:
        repo = "/Users/alice/src/sutando"
        self.assertEqual(_slug_new(repo), _slug_legacy(repo))

    def test_consecutive_special_chars_collapsed(self) -> None:
        self.assertEqual(_slug_new("/a//b___c/d"), "-a-b-c-d")

    def test_digits_preserved(self) -> None:
        self.assertEqual(_slug_new("/Users/user1/repo2"), "-Users-user1-repo2")


class MemoryDirFallbackTests(unittest.TestCase):

    def test_new_slug_preferred_when_both_exist(self) -> None:
        repo = "/Users/foo_bar/src/sutando"
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "projects" / _slug_new(repo) / "memory").mkdir(parents=True)
            (Path(td) / "projects" / _slug_legacy(repo) / "memory").mkdir(parents=True)
            result = _resolve_memory_dir(repo, td)
        self.assertIn(_slug_new(repo), result)
        self.assertNotIn(_slug_legacy(repo), result)

    def test_legacy_fallback_when_only_old_slug_exists(self) -> None:
        repo = "/Users/foo_bar/src/sutando"
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "projects" / _slug_legacy(repo) / "memory").mkdir(parents=True)
            result = _resolve_memory_dir(repo, td)
        self.assertIn(_slug_legacy(repo), result)

    def test_new_slug_returned_as_default_when_neither_exists(self) -> None:
        repo = "/Users/no_dir/src/sutando"
        result = _resolve_memory_dir(repo, "/nonexistent")
        self.assertIn(_slug_new(repo), result)

    def test_no_underscore_in_slug_for_underscore_username(self) -> None:
        """The resolved path's slug segment must not contain underscores."""
        repo = "/Users/xingyu_xiang/src/sutando"
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "projects" / _slug_new(repo) / "memory").mkdir(parents=True)
            result = _resolve_memory_dir(repo, td)
        slug_part = Path(result).parts[-3]  # …/projects/<slug>/memory
        self.assertNotIn("_", slug_part)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(SlugGenerationTests))
    suite.addTests(loader.loadTestsFromTestCase(MemoryDirFallbackTests))
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("\nAll health-check memory-dir slug tests passed.")
    sys.exit(0 if result.wasSuccessful() else 1)
