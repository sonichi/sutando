#!/usr/bin/env python3
"""An undeliverable proactive claim is released, not deleted.
Walks the AST: an unlink reachable from a non-delivery branch is structural."""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_recovery import (claim_for_delivery, recover_orphan_sending_files,
                                release_claim)  # noqa: E402


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

    def test_the_txt_vanishing_inside_the_claim_window_still_yields_a_claim(self):
        """A peer may remove the source between the link and the unlink.
        The claim is already made, so returning None would strand a held body."""
        real = Path.unlink

        def vanish(self_path, *a, **kw):
            if self_path == self.msg:
                raise FileNotFoundError(2, "vanished", str(self_path))
            return real(self_path, *a, **kw)

        with mock.patch.object(Path, "unlink", vanish):
            claim = claim_for_delivery(self.msg, "UOWNER")
        self.assertIsNotNone(claim, "dropped a claim it had already linked")
        self.assertTrue(claim.exists())
        self.assertEqual(claim.read_text(), "body")

    def test_a_lost_race_returns_none_rather_than_raising(self):
        """Another poller renamed it first; the caller must see None, not a traceback."""
        gone = self.box / "proactive-vanished.txt"
        self.assertIsNone(claim_for_delivery(gone, "UOWNER"))

    def test_claim_never_clobbers_an_existing_sending(self):
        """POSIX rename REPLACES the destination; an in-flight peer body must survive."""
        other = self.box / "proactive-x.sending"
        other.write_text("a peer's in-flight body")
        self.assertIsNone(claim_for_delivery(self.msg, "UOWNER"),
                          "claimed over an existing .sending")
        self.assertEqual(other.read_text(), "a peer's in-flight body",
                         "destroyed a peer's in-flight claim")
        self.assertTrue(self.msg.exists(), "consumed the .txt while losing the collision")
        self.assertEqual(self.msg.read_text(), "body")

    def test_release_never_clobbers_an_existing_txt(self):
        """Same hazard on the way back: a newer .txt must not be overwritten."""
        claim = self.box / "proactive-y.sending"
        claim.write_text("older claim body")
        newer = self.box / "proactive-y.txt"
        newer.write_text("newer body")
        self.assertFalse(release_claim(claim), "reported release over an existing .txt")
        self.assertEqual(newer.read_text(), "newer body", "clobbered the newer .txt")
        self.assertTrue(claim.exists(), "dropped the claim it could not release")

    def test_release_loses_the_check_then_act_race_without_clobbering(self):
        """An exists() guard cannot see a .txt that lands inside its own window.

        The guard is simulated as blind, which is what a concurrent writer makes
        it; only a no-clobber primitive survives, so this fails on rename.
        """
        claim = self.box / "proactive-z.sending"
        claim.write_text("older claim body")
        target = self.box / "proactive-z.txt"
        target.write_text("newer body")
        real_exists = Path.exists

        def blind(self_path):
            return False if self_path == target else real_exists(self_path)

        with mock.patch.object(Path, "exists", blind):
            released = release_claim(claim)
        self.assertEqual(target.read_text(), "newer body",
                         "clobbered a .txt the exists() guard could not see")
        self.assertFalse(released)

    def test_a_target_appearing_inside_the_syscall_window_is_not_clobbered(self):
        """The competing write lands BETWEEN the decision and the syscall.

        A pre-existing target is refused by check-then-act too; only a write
        arriving inside the window separates no-clobber from a guarded rename.
        """
        claim = self.box / "proactive-race.sending"
        claim.write_text("older claim body")
        target = self.box / "proactive-race.txt"
        self.assertFalse(target.exists(), "target must be ABSENT when the call begins")
        real_link = os.link

        def link_losing_the_window(src, dst):
            if Path(dst) == target and not target.exists():
                target.write_text("newer body")
            return real_link(src, dst)

        with mock.patch.object(os, "link", link_losing_the_window):
            released = release_claim(claim)

        self.assertEqual(target.read_text(), "newer body",
                         "a body that appeared inside the window was overwritten")
        self.assertFalse(released, "lost the race but reported released")
        self.assertTrue(claim.exists(), "claim discarded after losing the race")

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
    """Every branch body that runs when this bridge has no one to deliver to.

    THREE spellings, because resolving the recipient before claiming removes the
    need for an owner-absent branch after it: `if owner_id is None:` (body),
    `if owner_id is not None: ... else:` (orelse), and the guard-clause form
    `if claim is None:` that the delegating shape uses. Matching only the first
    two made this assertion report "stale" the moment a consumer adopted the
    third, which is the shape the fix is FOR.
    """
    found = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.If) or not isinstance(n.test, ast.Compare):
            continue
        left = n.test.left
        if not (isinstance(left, ast.Name) and left.id in ("owner_id", "claim")):
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

    def test_the_body_guard_runs_AFTER_the_claim(self):
        """The claim hard-links then unlinks, so a producer holding the original fd
        keeps writing that inode: a guard run only pre-claim inspects a body that is
        not the one sent. telegram had this; slack guarded the pre-claim peek only."""
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                tree = ast.parse(path.read_text())
                claims = [n.lineno for n in ast.walk(tree)
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name)
                          and n.func.id == "claim_for_delivery"]
                guards = [n.lineno for n in ast.walk(tree)
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name)
                          and n.func.id == "proactive_body_guard"]
                self.assertTrue(claims, f"{name}: no claim -- test is stale")
                self.assertTrue(guards, f"{name}: no body guard -- test is stale")
                self.assertTrue(
                    any(g > min(claims) for g in guards),
                    f"{name}: every proactive_body_guard call (lines {guards}) precedes "
                    f"the claim at line {min(claims)} -- the guarded body is not the "
                    f"body delivered",
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


class InterruptedClaimIsIdempotentTest(unittest.TestCase):
    """A crash BETWEEN the claim's link and unlink must not leave a double-send."""

    def test_both_names_on_one_inode_are_reconciled_to_the_txt(self):
        box = Path(tempfile.mkdtemp(prefix="claim-crash-"))
        txt = box / "proactive-crash.txt"
        txt.write_text("one body")
        claim = box / "proactive-crash.sending"
        os.link(txt, claim)          # exactly the state a crash after link leaves
        self.assertEqual(txt.stat().st_ino, claim.stat().st_ino)

        recover_orphan_sending_files(box)

        self.assertTrue(txt.exists(), "destroyed the only copy of the body")
        self.assertEqual(txt.read_text(), "one body")
        self.assertFalse(claim.exists(),
                         "left a .sending beside the .txt — the message is "
                         "visible to the poller AND held as a claim: double send")

    def test_a_REAL_collision_is_still_left_alone(self):
        """Different inodes mean two distinct bodies; recovery must not merge them."""
        box = Path(tempfile.mkdtemp(prefix="claim-collide-"))
        txt = box / "proactive-x.txt"; txt.write_text("new body")
        claim = box / "proactive-x.sending"; claim.write_text("older in-flight body")
        self.assertNotEqual(txt.stat().st_ino, claim.stat().st_ino)

        recover_orphan_sending_files(box)

        self.assertEqual(txt.read_text(), "new body")
        self.assertEqual(claim.read_text(), "older in-flight body")


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
