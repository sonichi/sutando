#!/usr/bin/env python3
"""Unit tests for skills/standing-approvals/scripts/standing_approvals_policy.py: the scope
grammar, matching (AND, globs, numeric bounds, fail-closed on missing keys), expiry, and which
rows count (Active, set by the owner after the last change to its terms)."""
import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "standing_approvals_policy", REPO / "skills" / "standing-approvals" / "scripts" / "standing_approvals_policy.py")
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(policy)

OWNER, AGENT = "@owner:example.org", "@agent:example.org"
NOW = datetime(2026, 11, 4, 12, 0)
Cell, Approval = policy.Cell, policy.Approval


def row(rid="r1", kind="merge PR", scope="repo: sonichi/*", status="Active", status_by=OWNER,
        terms_by=AGENT, terms_at=100, status_at=200, expires=None, created_by=AGENT):
    cells = {"kind": Cell(kind, terms_by, terms_at), "scope": Cell(scope, terms_by, terms_at),
             "status": Cell(status, status_by, status_at)}
    if expires is not None:
        cells["expires"] = Cell(expires, terms_by, terms_at)
    return Approval(rid, cells, created_by=created_by)


def attrs(*pairs):
    return policy.parse_attrs(pairs)


def matches(scope, *pairs):
    return policy.scope_matches(policy.parse_scope(scope), attrs(*pairs))[0]


class ScopeGrammar(unittest.TestCase):
    def test_lines_and_semicolons_are_conditions(self):
        s = policy.parse_scope("repo: sonichi/*\n# a comment\n\nbranch != main; amount <= 50")
        self.assertEqual([c.op for c in s.conditions], ["glob", "not_glob", "<="])
        self.assertEqual(s.conditions[2].bound, 50)

    def test_any_alone(self):
        self.assertTrue(policy.parse_scope("ANY").explicit_any)
        self.assertTrue(policy.parse_scope("any").any)

    def test_any_mixed_with_conditions_is_refused(self):
        with self.assertRaises(policy.ScopeError):
            policy.parse_scope("any\nrepo: x")

    def test_malformed_lines_are_refused(self):
        for bad in ("just words", "amount <= fifty", "repo:", "repo: a |", "9key: x", ": x"):
            with self.subTest(bad=bad), self.assertRaises(policy.ScopeError):
                policy.parse_scope(bad)

    def test_attrs_need_key_equals_value(self):
        self.assertEqual(attrs("to=a@x.com", "To=b@x.com"), {"to": ["a@x.com", "b@x.com"]})
        with self.assertRaises(ValueError):
            attrs("novalue")


class Matching(unittest.TestCase):
    def test_glob_case_insensitive_and_star_crosses_slash(self):
        self.assertTrue(matches("repo: sonichi/*", "repo=SoniChi/sutando"))
        self.assertTrue(matches("path: docs/*", "path=docs/a/b.md"))
        self.assertFalse(matches("repo: sonichi/*", "repo=other/sutando"))

    def test_alternatives_within_a_line(self):
        self.assertTrue(matches("to: *@a.com | *@b.com", "to=x@b.com"))
        self.assertFalse(matches("to: *@a.com | *@b.com", "to=x@c.com"))

    def test_lines_are_anded(self):
        scope = "repo: sonichi/*\nbranch: feat/*"
        self.assertTrue(matches(scope, "repo=sonichi/x", "branch=feat/y"))
        self.assertFalse(matches(scope, "repo=sonichi/x", "branch=main"))

    def test_every_value_of_a_repeated_key_must_fit(self):
        self.assertTrue(matches("to: *@a.com", "to=x@a.com", "to=y@a.com"))
        self.assertFalse(matches("to: *@a.com", "to=x@a.com", "to=y@evil.com"))

    def test_missing_key_fails_closed(self):
        self.assertFalse(matches("repo: sonichi/*", "branch=main"))
        self.assertFalse(matches("branch != main", "repo=sonichi/x"))
        self.assertFalse(matches("amount <= 50", "repo=x"))

    def test_negation(self):
        self.assertTrue(matches("branch != main | release/*", "branch=feat/x"))
        self.assertFalse(matches("branch != main | release/*", "branch=release/1"))

    def test_numeric_bounds(self):
        self.assertTrue(matches("amount <= 50", "amount=50"))
        self.assertFalse(matches("amount < 50", "amount=50"))
        self.assertTrue(matches("amount >= 1\namount <= 50", "amount=12.5"))
        self.assertFalse(matches("amount <= 50", "amount=$40"))
        self.assertFalse(matches("amount <= 50", "amount=nan"))
        self.assertFalse(matches("amount <= 50", "amount=inf"))

    def test_any_matches_without_attrs(self):
        self.assertTrue(matches("any"))


class Validity(unittest.TestCase):
    def ok(self, r, now=NOW):
        return policy.validity(r, OWNER, now)[0]

    def test_owner_activated_row_counts(self):
        self.assertTrue(self.ok(row()))

    def test_proposed_and_revoked_do_not_count(self):
        self.assertFalse(self.ok(row(status="Proposed")))
        self.assertFalse(self.ok(row(status="Revoked")))
        self.assertFalse(self.ok(row(status=None)))

    def test_active_set_by_someone_else_does_not_count(self):
        self.assertFalse(self.ok(row(status_by=AGENT)))
        self.assertFalse(self.ok(row(status_by=None)))

    def test_unknown_owner_counts_nothing(self):
        self.assertFalse(policy.validity(row(), "", NOW)[0])

    def test_terms_changed_by_another_after_activation_do_not_count(self):
        self.assertFalse(self.ok(row(terms_at=300, status_at=200)))
        self.assertFalse(self.ok(row(terms_at=None)))
        self.assertTrue(self.ok(row(terms_by=OWNER, terms_at=300, status_at=200)))

    def test_expiry(self):
        self.assertTrue(self.ok(row(expires={"start": "2026-11-04"})))  # through the end of that day
        self.assertFalse(self.ok(row(expires={"start": "2026-11-03"})))
        self.assertFalse(self.ok(row(expires="2026-11-04T11:59")))
        self.assertTrue(self.ok(row(expires="2026-11-04T12:01")))
        self.assertFalse(self.ok(row(expires="next week")))

    def test_bad_scope_does_not_count(self):
        self.assertFalse(self.ok(row(scope="any\nrepo: x")))

    def test_empty_scope_counts_only_on_the_owners_own_row(self):
        self.assertFalse(self.ok(row(scope="")))
        self.assertTrue(self.ok(row(scope="", created_by=OWNER)))
        self.assertTrue(self.ok(row(scope="any")))


class FindCover(unittest.TestCase):
    def test_kind_is_case_insensitive_exact(self):
        rows = [row()]
        a = attrs("repo=sonichi/sutando")
        self.assertEqual(policy.find_cover(rows, "Merge  PR", a, OWNER, NOW)[0].id, "r1")
        self.assertIsNone(policy.find_cover(rows, "merge PRs", a, OWNER, NOW)[0])

    def test_skips_non_counting_rows_and_reports_why(self):
        rows = [row("r1", status_by=AGENT), row("r2", scope="repo: other/*"), row("r3")]
        hit, misses = policy.find_cover(rows, "merge PR", attrs("repo=sonichi/sutando"), OWNER, NOW)
        self.assertEqual(hit.id, "r3")
        self.assertEqual([m.split(":")[0] for m in misses], ["r1", "r2"])

    def test_no_cover(self):
        hit, misses = policy.find_cover([row(status="Proposed")], "merge PR", attrs("repo=sonichi/x"), OWNER, NOW)
        self.assertIsNone(hit)
        self.assertEqual(len(misses), 1)


if __name__ == "__main__":
    unittest.main()
