#!/usr/bin/env python3
"""Every write_owner_activity() call in slack-bridge sits under an
`access_tier == "owner"` equality gate, after tier resolution and after redaction.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SLACK = REPO / "src" / "slack-bridge.py"
DISCORD = REPO / "src" / "discord-bridge.py"


def _lines(p: Path) -> list[str]:
    return p.read_text().splitlines()


def _line_of(p: Path, needle: str, skip_def: bool = True) -> int:
    for i, l in enumerate(_lines(p), 1):
        if needle in l and not (skip_def and l.lstrip().startswith("def ")):
            return i
    raise AssertionError(f"{needle!r} not found in {p.name}")


def _is_owner_eq_gate(test: "ast.expr") -> bool:
    """True only for a POSITIVE `access_tier == "owner"` equality — a negation,
    `!=`, or a widening `or` must not satisfy the gate."""
    for n in ast.walk(test):
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return False
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            return False
        if isinstance(n, ast.Compare) and any(not isinstance(o, ast.Eq) for o in n.ops):
            return False
    for n in ast.walk(test):
        if not isinstance(n, ast.Compare) or len(n.ops) != 1:
            continue
        left, right = n.left, n.comparators[0]
        names = {getattr(left, "id", None), getattr(right, "id", None)}
        consts = {getattr(left, "value", None), getattr(right, "value", None)}
        if "access_tier" in names and "owner" in consts:
            return True
    return False


class SlackOwnerPresenceGate(unittest.TestCase):
    def test_the_write_happens_after_the_tier_is_resolved(self):
        resolved = _line_of(SLACK, "access_tier = resolve_access_tier")
        write = _line_of(SLACK, "write_owner_activity(")
        self.assertGreater(
            write, resolved,
            "the presence write must not precede tier resolution — before this fix it "
            f"ran at {write} while the tier was only known at {resolved}",
        )

    def test_the_write_is_inside_an_owner_only_branch(self):
        """Walk the AST: the call must be dominated by `access_tier == "owner"`."""
        tree = ast.parse(SLACK.read_text())
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if not _is_owner_eq_gate(node.test):
                continue
            cond = ast.unparse(node.test)
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Call)
                        and getattr(inner.func, "id", "") == "write_owner_activity"):
                    found.append(cond)
        self.assertTrue(
            found, "no write_owner_activity() call is guarded by an access_tier == owner test"
        )

    def test_no_ungated_call_remains(self):
        """Every call site must sit under such a guard, not just one of them."""
        tree = ast.parse(SLACK.read_text())
        gated = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_owner_eq_gate(node.test):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call)
                            and getattr(inner.func, "id", "") == "write_owner_activity"):
                        gated.add(inner.lineno)
        all_calls = {n.lineno for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "write_owner_activity"}
        self.assertEqual(all_calls, gated, f"ungated call sites: {sorted(all_calls - gated)}")

    def test_redaction_still_precedes_the_write(self):
        """Moving the call must not move it above the secret filter."""
        redact = _line_of(SLACK, "initial_secret_filter = filter_chat_secrets")
        write = _line_of(SLACK, "write_owner_activity(")
        self.assertLess(redact, write, "a raw token must never reach state JSON")

    def test_discord_remains_the_pattern_being_matched(self):
        """If discord ever loses its gate, this fix has lost its precedent."""
        tree = ast.parse(DISCORD.read_text())
        ok = any(
            isinstance(node, ast.If)
            and "access_tier" in ast.unparse(node.test)
            and "owner" in ast.unparse(node.test)
            and any(isinstance(i, ast.Call) and getattr(i.func, "id", "") == "write_owner_activity"
                    for i in ast.walk(node))
            for node in ast.walk(tree)
        )
        self.assertTrue(ok, "discord-bridge no longer gates its presence write on owner tier")


class OwnerEqPredicate(unittest.TestCase):
    """The predicate must reject exactly what the substring version accepted."""

    def _t(self, src: str) -> "ast.expr":
        return ast.parse(src, mode="eval").body

    def test_accepts_the_positive_equality(self):
        self.assertTrue(_is_owner_eq_gate(self._t('access_tier == "owner"')))
        self.assertTrue(_is_owner_eq_gate(self._t('access_tier == "owner" and fresh')))

    def test_rejects_the_inversion(self):
        self.assertFalse(_is_owner_eq_gate(self._t('access_tier != "owner"')))
        self.assertFalse(_is_owner_eq_gate(self._t('not (access_tier == "owner")')))

    def test_rejects_a_widening_or(self):
        self.assertFalse(_is_owner_eq_gate(self._t('access_tier == "owner" or True')))

    def test_rejects_a_membership_test(self):
        self.assertFalse(_is_owner_eq_gate(self._t('"owner" in access_tier')))

if __name__ == "__main__":
    unittest.main()
