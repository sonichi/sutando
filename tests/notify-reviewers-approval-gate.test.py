#!/usr/bin/env python3
"""approval_gate: recruitment refuses approvals a PR already has (#3505).

Covers the gate's decision table without the network: the gh subprocess is
stubbed at subprocess.run, so every branch executes — met, unmet, COMMENTED
ignored, CR superseded by later APPROVED, query failure fails OPEN.
"""
import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location(
    "nr", REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
nr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nr)


def _gh(rows, rc=0, err=""):
    out = types.SimpleNamespace(returncode=rc, stdout=json.dumps(rows), stderr=err)
    return patch.object(nr.subprocess, "run", return_value=out)


class ApprovalGate(unittest.TestCase):
    def test_two_distinct_approvals_refuse(self):
        rows = [{"u": "a", "s": "APPROVED", "t": "2026-08-28T14:47:00Z"},
                {"u": "b", "s": "APPROVED", "t": "2026-08-28T13:18:00Z"}]
        with _gh(rows):
            met, detail = nr.approval_gate("1", "o/r")
        self.assertTrue(met)
        self.assertIn("a@", detail)
        self.assertIn("b@", detail)

    def test_one_approval_passes(self):
        rows = [{"u": "a", "s": "APPROVED", "t": "2026-08-28T14:47:00Z"}]
        with _gh(rows):
            met, detail = nr.approval_gate("1", "o/r")
        self.assertFalse(met)
        self.assertIn("1/2", detail)

    def test_commented_never_counts(self):
        rows = [{"u": "a", "s": "APPROVED", "t": "t1"},
                {"u": "b", "s": "COMMENTED", "t": "t2"}]
        with _gh(rows):
            met, _ = nr.approval_gate("1", "o/r")
        self.assertFalse(met)

    def test_later_approval_supersedes_own_cr(self):
        rows = [{"u": "a", "s": "CHANGES_REQUESTED", "t": "t1"},
                {"u": "a", "s": "APPROVED", "t": "t2"},
                {"u": "b", "s": "APPROVED", "t": "t3"}]
        with _gh(rows):
            met, _ = nr.approval_gate("1", "o/r")
        self.assertTrue(met)

    def test_later_cr_supersedes_own_approval(self):
        rows = [{"u": "a", "s": "APPROVED", "t": "t1"},
                {"u": "a", "s": "CHANGES_REQUESTED", "t": "t2"},
                {"u": "b", "s": "APPROVED", "t": "t3"}]
        with _gh(rows):
            met, _ = nr.approval_gate("1", "o/r")
        self.assertFalse(met)

    def test_query_failure_fails_open(self):
        with _gh([], rc=1, err="boom"):
            met, detail = nr.approval_gate("1", "o/r")
        self.assertFalse(met)
        self.assertIn("proceeding", detail)

    def test_exception_fails_open(self):
        with patch.object(nr.subprocess, "run", side_effect=OSError("no gh")):
            met, detail = nr.approval_gate("1", "o/r")
        self.assertFalse(met)
        self.assertIn("proceeding", detail)


if __name__ == "__main__":
    unittest.main(verbosity=1)
