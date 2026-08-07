#!/usr/bin/env python3
"""Only an owner-tier Slack sender may stamp the owner-PRESENCE signal.

`state/last-owner-activity.json` is not telemetry: the proactive loop reads it to
decide "owner active in the last ~5min, do not pre-empt" and "away >30min, staying
quiet is fine". A team/other sender writing it makes the loop treat a peer's message
as the owner sitting at his desk. discord-bridge already gates on
`access_tier == "owner"`; slack-bridge wrote before the tier was resolved.
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
            cond = ast.unparse(node.test)
            if "access_tier" not in cond or "owner" not in cond:
                continue
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
            cond = ast.unparse(node.test) if isinstance(node, ast.If) else ""
            if "access_tier" in cond and "owner" in cond:
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


if __name__ == "__main__":
    unittest.main()
