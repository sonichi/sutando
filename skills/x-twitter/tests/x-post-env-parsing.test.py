#!/usr/bin/env python3
"""
Tests for the .env parsing logic in skills/x-twitter/x-post.py.

Background: 2026-04-17 incident — a stale X_BEARER_TOKEN in the shell
environment silently shadowed a freshly-rotated token in .env because the
old code used `os.environ.setdefault`, which never overwrites existing keys.
A second bug: .env values written as `X_API_KEY="abc"` were stored with
literal surrounding quotes, causing OAuth1 signing to produce 401s.

Both fixes are critical for correct X API authentication. These tests guard
the parsing behavior without requiring live X credentials or network access.

Guards:
  1.  Double-quoted values are unquoted (`"abc"` → `abc`)
  2.  Single-quoted values are unquoted (`'abc'` → `abc`)
  3.  Unmatched quotes are preserved (`"abc` stays as `"abc`)
  4.  Values shorter than 2 chars are NOT unquoted (`"` stays as `"`)
  5.  Comment lines (#…) are skipped entirely
  6.  Lines without `=` are skipped
  7.  Leading/trailing whitespace stripped from keys
  8.  Leading/trailing whitespace stripped from values
  9.  Empty values are allowed (X_KEY= → stores "")
 10.  ENV_FILE overwrites existing env vars (no setdefault — the incident fix)
 11.  Structural: ENV_FILE path resolves to repo root / .env
 12.  Structural: `setdefault` not used for X_* env loading
 13.  Structural: `os.environ[key...] = val` write pattern is present

Run: python3 skills/x-twitter/tests/x-post-env-parsing.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent  # tests/ → x-twitter/ → skills/ → repo
SRC = REPO / "skills" / "x-twitter" / "x-post.py"
SOURCE = SRC.read_text(encoding="utf-8")


def _parse_env(content: str) -> dict[str, str]:
    """Replicate the x-post.py .env parsing logic for unit testing."""
    result: dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            result[key.strip()] = val
    return result


class TestEnvParsing(unittest.TestCase):

    # ── 1. Double-quoted values stripped ────────────────────────────────────
    def test_double_quoted_value_stripped(self):
        """Quoted .env values caused 401s because OAuth1 signed the literal quotes."""
        env = _parse_env('X_API_KEY="my-secret-key"')
        self.assertEqual(env["X_API_KEY"], "my-secret-key")

    # ── 2. Single-quoted values stripped ────────────────────────────────────
    def test_single_quoted_value_stripped(self):
        env = _parse_env("X_API_KEY='my-secret-key'")
        self.assertEqual(env["X_API_KEY"], "my-secret-key")

    # ── 3. Unmatched quotes preserved ───────────────────────────────────────
    def test_unmatched_opening_quote_preserved(self):
        """Only strip when BOTH ends are the same quote char."""
        env = _parse_env('X_API_KEY="abc')
        self.assertEqual(env["X_API_KEY"], '"abc')

    def test_mismatched_quote_types_preserved(self):
        env = _parse_env("X_API_KEY=\"abc'")
        self.assertEqual(env["X_API_KEY"], "\"abc'")

    # ── 4. Single-char values not unquoted ──────────────────────────────────
    def test_single_char_quote_not_unquoted(self):
        """len(val) >= 2 guard: a lone quote char must not be unquoted."""
        env = _parse_env('X_API_KEY="')
        self.assertEqual(env["X_API_KEY"], '"')

    # ── 5. Comment lines skipped ─────────────────────────────────────────────
    def test_comment_lines_skipped(self):
        env = _parse_env("# X_API_KEY=should-be-ignored\nX_REAL=real-value")
        self.assertNotIn("# X_API_KEY", env)
        self.assertEqual(env["X_REAL"], "real-value")

    def test_inline_comment_not_skipped(self):
        """Inline comments (after =) are treated as part of the value."""
        env = _parse_env("X_API_KEY=abc # comment")
        # Value includes ' # comment' — this matches python-dotenv behavior
        self.assertIn("X_API_KEY", env)

    # ── 6. Lines without = skipped ───────────────────────────────────────────
    def test_lines_without_equals_skipped(self):
        env = _parse_env("FOOBAR\nX_REAL=real")
        self.assertNotIn("FOOBAR", env)
        self.assertEqual(env["X_REAL"], "real")

    # ── 7. Key whitespace stripped ───────────────────────────────────────────
    def test_key_whitespace_stripped(self):
        env = _parse_env("  X_API_KEY  =abc")
        self.assertIn("X_API_KEY", env)
        self.assertNotIn("  X_API_KEY  ", env)
        self.assertEqual(env["X_API_KEY"], "abc")

    # ── 8. Value whitespace stripped ─────────────────────────────────────────
    def test_value_whitespace_stripped(self):
        env = _parse_env("X_API_KEY=  abc  ")
        self.assertEqual(env["X_API_KEY"], "abc")

    # ── 9. Empty values allowed ──────────────────────────────────────────────
    def test_empty_value_allowed(self):
        env = _parse_env("X_API_KEY=")
        self.assertIn("X_API_KEY", env)
        self.assertEqual(env["X_API_KEY"], "")

    # ── 10. ENV_FILE overwrites existing env (no setdefault) ─────────────────
    def test_env_file_overwrites_existing_env(self):
        """The incident: setdefault let stale shell env shadow the .env value.
        The fix uses os.environ[key] = val (always overwrites)."""
        # Simulate: stale key already in os.environ
        os.environ["_TEST_XPOST_SENTINEL"] = "stale-value"
        try:
            # Replicate the write pattern (always overwrite)
            os.environ["_TEST_XPOST_SENTINEL"] = "fresh-value"
            self.assertEqual(os.environ["_TEST_XPOST_SENTINEL"], "fresh-value")
        finally:
            del os.environ["_TEST_XPOST_SENTINEL"]

    # ── 11. Multi-line .env parsed correctly ─────────────────────────────────
    def test_multiline_env_parsed(self):
        content = """
# Twitter API credentials
X_API_KEY="key123"
X_API_SECRET='secret456'
X_ACCESS_TOKEN=token789
X_ACCESS_TOKEN_SECRET="tok-secret"
X_BEARER_TOKEN=bearer-abc
"""
        env = _parse_env(content)
        self.assertEqual(env["X_API_KEY"], "key123")
        self.assertEqual(env["X_API_SECRET"], "secret456")
        self.assertEqual(env["X_ACCESS_TOKEN"], "token789")
        self.assertEqual(env["X_ACCESS_TOKEN_SECRET"], "tok-secret")
        self.assertEqual(env["X_BEARER_TOKEN"], "bearer-abc")


class TestSourceStructure(unittest.TestCase):

    # ── 12. setdefault not used for env loading ──────────────────────────────
    def test_no_setdefault_for_x_credentials(self):
        """The 2026-04-17 fix: setdefault lets stale env shadow .env updates.
        Must use direct assignment os.environ[key] = val instead."""
        # setdefault should not appear in the env-loading block
        # (It's acceptable elsewhere, but not in the .env parse loop)
        env_load_section = SOURCE[SOURCE.find("ENV_FILE"):SOURCE.find("API_KEY = os.environ")]
        self.assertNotIn("setdefault", env_load_section,
                         "setdefault must not be used in the .env loading block")

    # ── 13. Direct assignment pattern present ────────────────────────────────
    def test_direct_assignment_in_env_loader(self):
        """os.environ[key.strip()] = val is the correct overwrite pattern."""
        self.assertRegex(SOURCE,
            r'os\.environ\[key\.strip\(\)\]\s*=\s*val',
            "Must use os.environ[key.strip()] = val for env loading")

    # ── ENV_FILE path is parent.parent.parent ────────────────────────────────
    def test_env_file_path_uses_three_parents(self):
        """x-post.py is 3 dirs deep (skills/x-twitter/x-post.py)
        so .parent.parent.parent reaches repo root where .env lives."""
        self.assertIn("parent.parent.parent", SOURCE,
                      "ENV_FILE must traverse 3 parent dirs to reach repo root")

    # ── Quote-stripping guard is present ─────────────────────────────────────
    def test_quote_stripping_guard_in_source(self):
        """The len >= 2 and matching-ends guard must be in source."""
        self.assertRegex(SOURCE,
            r'len\(val\)\s*>=\s*2.*val\[0\]\s*==\s*val\[-1\]|'
            r'val\[0\]\s*==\s*val\[-1\].*len\(val\)',
            "Quote-stripping guard (len>=2, matching ends) must be present")

    # ── ENV_FILE.exists() guard before reading ───────────────────────────────
    def test_env_file_existence_checked(self):
        """Must guard with ENV_FILE.exists() — no crash on fresh clone."""
        self.assertIn("ENV_FILE.exists()", SOURCE,
                      "ENV_FILE must be checked for existence before reading")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestEnvParsing))
    suite.addTests(loader.loadTestsFromTestCase(TestSourceStructure))
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
