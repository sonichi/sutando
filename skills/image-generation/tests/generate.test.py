#!/usr/bin/env python3
"""
Tests for skills/image-generation/scripts/generate.py

Coverage:
  load_env — reads KEY=VALUE, skips comments and blanks, strips surrounding quotes
             (double and single), .env overwrites stale shell env, handles missing file

Run: python3 skills/image-generation/tests/generate.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

# generate.py imports google.genai and PIL at function call time (inside
# generate_image/generate_video), not at module level — so loading is safe.
_spec = importlib.util.spec_from_file_location(
    "generate",
    REPO / "skills" / "image-generation" / "scripts" / "generate.py",
)
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

_TMPDIR = Path(tempfile.mkdtemp())
_UNIQUE_KEY = "_SUTANDO_TEST_LOAD_ENV_12345"


def _write_env(content: str) -> Path:
    p = _TMPDIR / ".env"
    p.write_text(content)
    return _TMPDIR


def _clear_key():
    os.environ.pop(_UNIQUE_KEY, None)


# ── load_env ──────────────────────────────────────────────────────────────────

class TestLoadEnv(unittest.TestCase):
    def setUp(self):
        _clear_key()

    def tearDown(self):
        _clear_key()

    def _load(self, tmpdir):
        """Call load_env with home dir pointing at tmpdir (so ~/.env is picked up)."""
        with patch("pathlib.Path.home", return_value=tmpdir):
            ge.load_env()

    def test_plain_key_value_set(self):
        tmpdir = _write_env(f"{_UNIQUE_KEY}=hello\n")
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "hello")

    def test_double_quoted_value_stripped(self):
        tmpdir = _write_env(f'{_UNIQUE_KEY}="quoted_value"\n')
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "quoted_value")

    def test_single_quoted_value_stripped(self):
        tmpdir = _write_env(f"{_UNIQUE_KEY}='single_quoted'\n")
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "single_quoted")

    def test_comment_line_skipped(self):
        tmpdir = _write_env(f"# {_UNIQUE_KEY}=should_not_set\n")
        self._load(tmpdir)
        self.assertIsNone(os.environ.get(_UNIQUE_KEY))

    def test_blank_line_skipped(self):
        tmpdir = _write_env(f"\n\n{_UNIQUE_KEY}=after_blank\n")
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "after_blank")

    def test_value_with_equals_sign_preserved(self):
        tmpdir = _write_env(f"{_UNIQUE_KEY}=a=b=c\n")
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "a=b=c")

    def test_missing_env_file_no_crash(self):
        empty_dir = Path(tempfile.mkdtemp())
        try:
            self._load(empty_dir)  # no .env file there
        except Exception as e:
            self.fail(f"load_env raised unexpectedly: {e}")

    def test_env_overwrites_stale_shell_env(self):
        os.environ[_UNIQUE_KEY] = "stale_value"
        tmpdir = _write_env(f"{_UNIQUE_KEY}=fresh_value\n")
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "fresh_value")

    def test_key_whitespace_stripped(self):
        tmpdir = _write_env(f"  {_UNIQUE_KEY}  =spaced\n")
        self._load(tmpdir)
        self.assertEqual(os.environ.get(_UNIQUE_KEY), "spaced")


if __name__ == "__main__":
    unittest.main()
