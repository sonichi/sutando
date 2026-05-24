#!/usr/bin/env python3
"""
Tests for skills/macos-tools/scripts/contacts.py

Coverage:
  search_contacts — single contact with email+phone, multi-email,
                    dedup on same name, error response, empty result,
                    partial line skipped, email-only contact

Run: python3 skills/macos-tools/tests/contacts.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "contacts",
    REPO / "skills" / "macos-tools" / "scripts" / "contacts.py",
)
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


def _fake_run(stdout="", returncode=0):
    """Return a mock for subprocess.run that emits the given output."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def _contact_line(name="Bob Smith", emails="bob@example.com,", phones="555-1234,"):
    return f"{name}|||{emails}|||{phones}"


def _mock_subprocess(stdout="", returncode=0):
    """Patch subprocess.run for both the open-Contacts call and the osascript call."""
    side_effects = [
        _fake_run("", 0),           # open -ga Contacts
        _fake_run(stdout, returncode),  # osascript
    ]
    return patch("subprocess.run", side_effect=side_effects)


# ── search_contacts ───────────────────────────────────────────────────────────

class TestSearchContacts(unittest.TestCase):
    def test_single_contact_with_email_and_phone(self):
        line = _contact_line("Bob Smith", "bob@x.com,", "555-1234,")
        with _mock_subprocess(line):
            result = ct.search_contacts("Bob")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Bob Smith")
        self.assertIn("bob@x.com", result[0]["emails"])
        self.assertIn("555-1234", result[0]["phones"])

    def test_multi_email_contact(self):
        line = _contact_line("Alice", "a@work.com,a@home.com,", "")
        with _mock_subprocess(line):
            result = ct.search_contacts("Alice")
        self.assertEqual(len(result[0]["emails"]), 2)
        self.assertIn("a@work.com", result[0]["emails"])
        self.assertIn("a@home.com", result[0]["emails"])

    def test_email_only_no_phone(self):
        line = _contact_line("Carol", "carol@x.com,", "")
        with _mock_subprocess(line):
            result = ct.search_contacts("Carol")
        self.assertEqual(result[0]["phones"], [])

    def test_duplicate_name_deduplicated(self):
        lines = "\n".join([
            _contact_line("Bob Smith", "b1@x.com,", "111,"),
            _contact_line("Bob Smith", "b2@x.com,", "222,"),
        ])
        with _mock_subprocess(lines):
            result = ct.search_contacts("Bob")
        # Only the first occurrence survives dedup
        bob_contacts = [c for c in result if c["name"] == "Bob Smith"]
        self.assertEqual(len(bob_contacts), 1)

    def test_empty_result_returns_empty_list(self):
        with _mock_subprocess(""):
            result = ct.search_contacts("NoOne")
        self.assertEqual(result, [])

    def test_error_returns_error_dict(self):
        m_open = _fake_run("", 0)
        m_script = _fake_run("", 1)
        m_script.stderr = "Contacts is not running"
        with patch("subprocess.run", side_effect=[m_open, m_script]):
            result = ct.search_contacts("Bob")
        self.assertEqual(len(result), 1)
        self.assertIn("error", result[0])

    def test_single_separator_line_creates_contact_without_phones(self):
        # "name|||emails" (2 parts, no phone segment) still creates contact
        # phones defaults to [] when len(parts) <= 2
        with _mock_subprocess("Bob Smith|||bob@x.com,"):
            result = ct.search_contacts("Bob")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["phones"], [])

    def test_zero_separator_line_skipped(self):
        # A line with no "|||" separator (1 part) should be ignored
        with _mock_subprocess("Just a plain line"):
            result = ct.search_contacts("Bob")
        self.assertEqual(result, [])

    def test_multiple_contacts(self):
        lines = "\n".join([
            _contact_line("Alice", "a@x.com,", ""),
            _contact_line("Bob", "b@x.com,", ""),
        ])
        with _mock_subprocess(lines):
            result = ct.search_contacts("")
        names = [c["name"] for c in result]
        self.assertIn("Alice", names)
        self.assertIn("Bob", names)

    def test_trailing_comma_stripped_from_emails(self):
        line = _contact_line("Dave", "dave@x.com,", "")
        with _mock_subprocess(line):
            result = ct.search_contacts("Dave")
        # No empty string from trailing comma
        self.assertNotIn("", result[0]["emails"])

    def test_whitespace_in_name_stripped(self):
        line = _contact_line("  Eve  ", "e@x.com,", "")
        with _mock_subprocess(line):
            result = ct.search_contacts("Eve")
        self.assertEqual(result[0]["name"], "Eve")


if __name__ == "__main__":
    unittest.main()
