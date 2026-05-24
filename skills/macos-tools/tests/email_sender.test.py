#!/usr/bin/env python3
"""
Tests for skills/macos-tools/scripts/email-sender.py

Coverage:
  escape    — backslash, double-quote, newline escaping
  send      — success (prints sent), draft mode (prints draft), cc included,
              non-zero returncode exits 1

Run: python3 skills/macos-tools/tests/email_sender.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "email_sender",
    REPO / "skills" / "macos-tools" / "scripts" / "email-sender.py",
)
es = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es)


def _fake_run(returncode=0, stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    return m


def _mock_run(returncode=0, stderr=""):
    return patch("subprocess.run", return_value=_fake_run(returncode, stderr))


# ── escape ────────────────────────────────────────────────────────────────────

class TestEscape(unittest.TestCase):
    def test_backslash_escaped(self):
        self.assertEqual(es.escape("a\\b"), "a\\\\b")

    def test_double_quote_escaped(self):
        self.assertEqual(es.escape('say "hi"'), 'say \\"hi\\"')

    def test_newline_escaped(self):
        self.assertEqual(es.escape("line1\nline2"), "line1\\nline2")

    def test_plain_text_unchanged(self):
        self.assertEqual(es.escape("hello world"), "hello world")

    def test_combined_special_chars(self):
        # Input has backslash and quote; both must be escaped
        result = es.escape('back\\slash and "quote"')
        self.assertIn('\\\\', result)   # backslash escaped to \\
        self.assertIn('\\"', result)    # quote escaped to \"


# ── send ──────────────────────────────────────────────────────────────────────

class TestSend(unittest.TestCase):
    def test_success_prints_sent(self):
        with _mock_run(), patch("sys.stdout", new_callable=StringIO) as out:
            es.send("to@x.com", "Subject", "Body")
        self.assertIn("sent", out.getvalue().lower())

    def test_draft_mode_prints_draft(self):
        with _mock_run(), patch("sys.stdout", new_callable=StringIO) as out:
            es.send("to@x.com", "Subj", "Body", draft=True)
        self.assertIn("draft", out.getvalue().lower())

    def test_cc_included_in_script(self):
        with patch("subprocess.run", return_value=_fake_run()) as mock_run, \
             patch("sys.stdout", new_callable=StringIO):
            es.send("to@x.com", "Subj", "Body", cc="cc@x.com")
        script_arg = mock_run.call_args[0][0]
        # The script passed to osascript should reference the cc address
        self.assertIn("cc@x.com", script_arg[-1])  # osascript -e <script>

    def test_error_exits_1(self):
        with _mock_run(returncode=1, stderr="Mail error"), \
             patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit) as ctx:
                es.send("to@x.com", "Subj", "Body")
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
