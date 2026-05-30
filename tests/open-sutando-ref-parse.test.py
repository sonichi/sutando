#!/usr/bin/env python3
"""Tests for parse_ref() and resolve_numeric() in skills/open-sutando-ref/scripts/resolve.py.

Pure-function tests that require no network, no gh CLI, no mocking.
Locks in the parsing behaviour so refactors stay honest.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESOLVE_PY = REPO / "skills" / "open-sutando-ref" / "scripts" / "resolve.py"


def load_module():
    spec = importlib.util.spec_from_file_location("resolve", RESOLVE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestParseRef(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not RESOLVE_PY.exists():
            raise unittest.SkipTest(f"{RESOLVE_PY} not found (skill not installed)")
        cls.m = load_module()

    def _parse(self, arg):
        return self.m.parse_ref(arg)

    # ── numeric forms ──────────────────────────────────────────────────────────

    def test_bare_number(self):
        self.assertEqual(self._parse("874"), ("numeric", 874, None))

    def test_hash_number(self):
        self.assertEqual(self._parse("#874"), ("numeric", 874, None))

    def test_hash_number_with_spaces(self):
        self.assertEqual(self._parse("  #874  "), ("numeric", 874, None))

    # ── PR forms ───────────────────────────────────────────────────────────────

    def test_pr_uppercase(self):
        self.assertEqual(self._parse("PR 874"), ("pr", 874, None))

    def test_pr_lowercase(self):
        self.assertEqual(self._parse("pr 874"), ("pr", 874, None))

    def test_pr_hash(self):
        self.assertEqual(self._parse("PR#874"), ("pr", 874, None))

    def test_pull_request(self):
        self.assertEqual(self._parse("pull request 874"), ("pr", 874, None))

    def test_pullrequest_nospace(self):
        self.assertEqual(self._parse("pullrequest 874"), ("pr", 874, None))

    # ── issue forms ────────────────────────────────────────────────────────────

    def test_issue_number(self):
        self.assertEqual(self._parse("issue 874"), ("issue", 874, None))

    def test_issue_hash(self):
        self.assertEqual(self._parse("issue #874"), ("issue", 874, None))

    def test_issue_uppercase(self):
        self.assertEqual(self._parse("ISSUE 874"), ("issue", 874, None))

    # ── fuzzy forms ────────────────────────────────────────────────────────────

    def test_fuzzy_description(self):
        ref_type, number, query = self._parse("the result-marker PR")
        self.assertEqual(ref_type, "fuzzy")
        self.assertIsNone(number)
        self.assertEqual(query, "the result-marker PR")

    def test_fuzzy_empty_is_fuzzy(self):
        # An empty string after strip() falls through to fuzzy
        ref_type, number, query = self._parse("some description")
        self.assertEqual(ref_type, "fuzzy")

    # ── resolve_numeric URL format ─────────────────────────────────────────────

    def test_resolve_numeric_pr(self):
        url = self.m.resolve_numeric("pr", 874, "sonichi/sutando")
        self.assertEqual(url, "https://github.com/sonichi/sutando/pull/874")

    def test_resolve_numeric_issue(self):
        url = self.m.resolve_numeric("issue", 874, "sonichi/sutando")
        self.assertEqual(url, "https://github.com/sonichi/sutando/issues/874")

    def test_resolve_numeric_different_repo(self):
        url = self.m.resolve_numeric("pr", 1, "foo/bar")
        self.assertEqual(url, "https://github.com/foo/bar/pull/1")


if __name__ == "__main__":
    unittest.main()
