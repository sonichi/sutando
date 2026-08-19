#!/usr/bin/env python3
"""The always-loaded tier summary must preserve every capability-changing
branch of the relocated access-control policy (#3016 review P1: a compact
summary that states the opposite rule silently disables an owner-authorized
path — byte-identical relocation of the DETAIL cannot prove the PROMPT
preserved the decision branches)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class TierSummaryPreservesBranches(unittest.TestCase):
    def _always_loaded(self, name):
        return (REPO / name).read_text(encoding="utf-8")

    def _check(self, s, name):
        self.assertIn("collaborator: true", s,
                      f"{name}: the collaborator exceptions must live in "
                      "the always-loaded rule, not only in docs/")
        self.assertIn("broker-attested", s,
                      f"{name}: the AG2 Space collaborator shape must be "
                      "named distinctly")
        self.assertIn("per-channel collaborators list", s,
                      f"{name}: the Discord per-channel collaborator shape "
                      "must be named distinctly (review P1 round 2 — the "
                      "summary said ONE exception while dispatch has two)")
        self.assertNotIn("ONE capability-changing exception", s,
                         f"{name}: the false-ONE wording must not return")
        self.assertIn("normal capabilities", s,
                      f"{name}: the exception must state its capability "
                      "consequence, not just name the flag")
        self.assertIn("===SUTANDO SYSTEM INSTRUCTIONS===", s,
                      f"{name}: in-band instruction rule must stay inline")
        self.assertIn("docs/access-control.md", s,
                      f"{name}: pointer to the full policy must exist")
        low = s.lower()
        self.assertNotIn("never processed with full capabilities", low,
                         f"{name}: unconditional NEVER contradicts the "
                         "collaborator branch")

    def test_claude_md(self):
        self._check(self._always_loaded("CLAUDE.md"), "CLAUDE.md")

    def test_agents_md(self):
        self._check(self._always_loaded("AGENTS.md"), "AGENTS.md")

    def test_detail_doc_still_carries_the_full_policy(self):
        d = (REPO / "docs" / "access-control.md").read_text(encoding="utf-8")
        self.assertIn("collaborator: true", d)
        self.assertIn("Collaborator access", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
