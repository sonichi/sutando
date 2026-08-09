#!/usr/bin/env python3
"""An undeliverable proactive claim is released, not deleted.

Walks the AST: the defect is an `unlink` reachable from a non-delivery branch,
which is a structural property a source regex cannot see.
"""
from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_recovery import release_claim  # noqa: E402

CONSUMERS = {
    "slack-bridge": REPO / "src" / "slack-bridge.py",
    "telegram-bridge": REPO / "src" / "telegram-bridge.py",
}


def _calls(node: ast.AST) -> set[str]:
    """Attribute names of every call in this subtree (e.g. {'unlink', ...})."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            if isinstance(fn, ast.Attribute):
                out.add(fn.attr)
            elif isinstance(fn, ast.Name):
                out.add(fn.id)
    return out


def _owner_none_branches(tree: ast.AST) -> list[ast.AST]:
    """Every branch body that runs when the resolved proactive owner is absent.

    Matches both spellings the bridges use: `if owner_id is None:` (body) and
    `if owner_id is not None: ... else:` (orelse).
    """
    found = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.If) or not isinstance(n.test, ast.Compare):
            continue
        left = n.test.left
        if not (isinstance(left, ast.Name) and left.id == "owner_id"):
            continue
        if len(n.test.ops) != 1 or not isinstance(n.test.comparators[0], ast.Constant):
            continue
        if n.test.comparators[0].value is not None:
            continue
        op = n.test.ops[0]
        if isinstance(op, ast.Is):
            found.append(ast.Module(body=n.body, type_ignores=[]))
        elif isinstance(op, ast.IsNot) and n.orelse:
            found.append(ast.Module(body=n.orelse, type_ignores=[]))
    return found


class UndeliverableClaimDelegationTest(unittest.TestCase):
    def test_no_owner_branch_releases_and_never_deletes(self):
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                branches = _owner_none_branches(ast.parse(path.read_text()))
                self.assertTrue(
                    branches, f"{name}: no owner-absent branch found -- test is stale"
                )
                for branch in branches:
                    calls = _calls(branch)
                    self.assertNotIn(
                        "unlink", calls,
                        f"{name}: deletes the claim when no owner is configured -- "
                        f"that destroys a message another bridge could deliver",
                    )
                    self.assertIn(
                        "release_claim", calls,
                        f"{name}: does not hand the claim back via "
                        f"proactive_recovery.release_claim",
                    )

    def test_a_raised_send_releases_and_never_deletes(self):
        """The handler for a failed proactive send must not consume the claim."""
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                tree = ast.parse(path.read_text())
                checked = 0
                for parent in ast.walk(tree):
                    body = getattr(parent, "body", None)
                    if not isinstance(body, list):
                        continue
                    for i, stmt in enumerate(body):
                        if not isinstance(stmt, ast.Try):
                            continue
                        # Match on the log tag both the fixed and broken forms
                        # share; keying on "release" would only ever match the fix.
                        handlers = [h for h in stmt.handlers
                                    if "[proactive]" in ast.unparse(h)
                                    and "fail" in ast.unparse(h).lower()]
                        if not handlers:
                            continue
                        checked += 1
                        for handler in handlers:
                            self.assertNotIn(
                                "unlink", _calls(handler),
                                f"{name}: deletes the claim after a send that raised",
                            )
                        # A delete placed AFTER the try runs on the raised path too,
                        # which is the same fallthrough defect one level out.
                        for follower in body[i + 1:]:
                            self.assertNotIn(
                                "unlink", _calls(follower),
                                f"{name}: deletes the claim after the proactive send "
                                f"block -- a raised send is destroyed, not retried",
                            )
                self.assertTrue(
                    checked, f"{name}: found no proactive send failure handler"
                )

    def test_no_unlink_runs_after_the_owner_branch_completes(self):
        """Pins the exact regression: the delete sat OUTSIDE the if/else.

        At the poll block's outer indent it ran for delivered, failed, and
        unaddressed alike. A branch-scoped walk cannot see that -- the statement
        is a *sibling* of the owner branch, so this checks the sibling list.
        """
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                tree = ast.parse(path.read_text())
                checked = 0
                for parent in ast.walk(tree):
                    body = getattr(parent, "body", None)
                    if not isinstance(body, list):
                        continue
                    for i, stmt in enumerate(body):
                        if not (isinstance(stmt, ast.If)
                                and isinstance(stmt.test, ast.Compare)
                                and isinstance(stmt.test.left, ast.Name)
                                and stmt.test.left.id == "owner_id"):
                            continue
                        checked += 1
                        # A guard clause that `continue`s makes the followers the
                        # owner-PRESENT path, where a delete is correct.
                        if isinstance(stmt.body[-1], (ast.Continue, ast.Return, ast.Raise)):
                            continue
                        for follower in body[i + 1:]:
                            self.assertNotIn(
                                "unlink", _calls(follower),
                                f"{name}: deletes the claim after the owner branch "
                                f"-- runs on delivered, failed and unaddressed alike",
                            )
                self.assertTrue(checked, f"{name}: no owner branch found -- test is stale")


class ReleasedClaimIsRecoverableTest(unittest.TestCase):
    """The released file must be visible to the pollers, which scan `.txt` only."""

    def test_released_undeliverable_claim_returns_to_the_txt_stream(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "proactive-1785904100.txt"
            src.write_text("a message this bridge cannot deliver")
            claim = src.with_suffix(".sending")
            src.rename(claim)

            self.assertTrue(release_claim(claim))
            self.assertFalse(claim.exists(), "claim left in the .sending namespace")
            restored = claim.with_suffix(".txt")
            self.assertTrue(restored.exists(), "no bridge can ever see this file again")
            self.assertEqual(restored.read_text(), "a message this bridge cannot deliver")


if __name__ == "__main__":
    unittest.main(verbosity=2)
