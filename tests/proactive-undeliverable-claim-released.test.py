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

from proactive_recovery import claim_for_delivery, release_claim  # noqa: E402


class ClaimForDeliveryTest(unittest.TestCase):
    """Direct contract for the shared gate — the adapters' AST checks never RUN it."""

    def setUp(self):
        self.box = Path(tempfile.mkdtemp(prefix="claim-gate-"))
        self.msg = self.box / "proactive-x.txt"
        self.msg.write_text("body")

    def test_no_recipient_leaves_the_txt_untouched(self):
        self.assertIsNone(claim_for_delivery(self.msg, None))
        self.assertTrue(self.msg.exists(), "claimed despite having no recipient")
        self.assertEqual(self.msg.read_text(), "body")
        self.assertFalse(list(self.box.glob("*.sending")))

    def test_a_recipient_claims_it(self):
        claim = claim_for_delivery(self.msg, "UOWNER")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.suffix, ".sending")
        self.assertTrue(claim.exists())
        self.assertFalse(self.msg.exists(), "left the .txt behind — a peer could double-send")

    def test_a_lost_race_returns_none_rather_than_raising(self):
        """Another poller renamed it first; the caller must see None, not a traceback."""
        gone = self.box / "proactive-vanished.txt"
        self.assertIsNone(claim_for_delivery(gone, "UOWNER"))

    def test_falsy_but_present_recipient_still_claims(self):
        """Only None means 'no recipient' — 0 or '' are recipients a provider may use."""
        claim = claim_for_delivery(self.msg, 0)
        self.assertIsNotNone(claim, "treated a falsy-but-present recipient as absent")

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
    def test_no_owner_branch_never_claims_and_never_deletes(self):
        """No recipient => the `.txt` stays exactly where a peer bridge polls.

        This asserted "release what you claimed". Resolving the recipient BEFORE
        claiming reaches the same property with no claim to release, so assert the
        property: the owner-absent branch neither deletes nor claims, and the claim
        itself is delegated so one module owns the decision.
        """
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                tree = ast.parse(path.read_text())
                branches = _owner_none_branches(tree)
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
                    self.assertNotIn(
                        "rename", calls,
                        f"{name}: claims the file on the owner-absent path -- a claim "
                        f"renames it out of the *.txt glob a peer bridge polls",
                    )
                self.assertIn(
                    "claim_for_delivery", _calls(tree),
                    f"{name}: claims inline instead of delegating to "
                    f"proactive_recovery.claim_for_delivery",
                )

    def test_the_recipient_is_resolved_before_the_claim(self):
        """Ordering IS the fix. A delegated claim placed first is still a claim."""
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                tree = ast.parse(path.read_text())
                resolves = [n.lineno for n in ast.walk(tree)
                            if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Name)
                            and n.func.id.endswith("resolve_proactive_owner_id")]
                claims = [n.lineno for n in ast.walk(tree)
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name)
                          and n.func.id == "claim_for_delivery"]
                self.assertTrue(resolves, f"{name}: no owner resolution found -- test is stale")
                self.assertTrue(claims, f"{name}: no claim_for_delivery call found")
                self.assertLess(
                    min(resolves), min(claims),
                    f"{name}: claims at line {min(claims)} before resolving the owner at "
                    f"line {min(resolves)} -- the claim hides the file from peers first",
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
