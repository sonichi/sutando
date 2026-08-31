#!/usr/bin/env python3
"""Tests for util_paths.claude_project_slug() (core-memory slug bug).

Every project-slug consumer used to re-implement the "dash every
non-alphanumeric character" regex inline, and drifted: health-check.py,
voice-agent.ts, and voice-context.ts all only replaced "/" — silently
resolving to a nonexistent projects/<slug>/ dir on any install path
containing a space or dot (e.g. a desktop-bundled checkout under
"Application Support/space.ag2.app/"). This locks the ONE correct
derivation behind a single shared helper so it can't drift again.

Run: python3 tests/util-paths-project-slug.test.py
Exit: 0 on pass, 1 on fail.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from util_paths import claude_project_slug  # noqa: E402


class ClaudeProjectSlugTests(unittest.TestCase):
    def test_plain_path_matches_slash_only_baseline(self):
        # No spaces/dots: identical to the old (broken) "/" -> "-" behavior,
        # so the fix is purely additive for the common case.
        self.assertEqual(
            claude_project_slug("/Users/foo/bar"), "-Users-foo-bar")

    def test_space_is_dashed(self):
        self.assertEqual(
            claude_project_slug("/Users/foo/My Documents/bar"),
            "-Users-foo-My-Documents-bar")

    def test_dot_is_dashed(self):
        self.assertEqual(
            claude_project_slug("/Users/foo/space.ag2.app/bar"),
            "-Users-foo-space-ag2-app-bar")

    def test_desktop_bundled_engine_path(self):
        # The exact real-world path class that triggered the report: a
        # desktop-bundled install with both a space ("Application Support")
        # and dots ("space.ag2.app") in the same path.
        bundled = "/Users/u/Library/Application Support/space.ag2.app/engine/sutando"
        self.assertEqual(
            claude_project_slug(bundled),
            "-Users-u-Library-Application-Support-space-ag2-app-engine-sutando")

    def test_accepts_path_object(self):
        self.assertEqual(
            claude_project_slug(Path("/Users/foo/bar")), "-Users-foo-bar")

    def test_alphanumerics_preserved(self):
        self.assertEqual(claude_project_slug("abc123"), "abc123")


if __name__ == "__main__":
    unittest.main()
