#!/usr/bin/env python3
"""Tests for skills/email-find/SKILL.md

Port of sonichi/sutando#1020. Verifies the skill file exists and documents
the required phases and behavioral rules.

Run: python3 tests/email-find-skill.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "skills" / "email-find" / "SKILL.md"
BUILT_IN_TOOLS = ROOT / "docs" / "built-in-tools.md"


class TestEmailFindSkillMd(unittest.TestCase):
    def setUp(self):
        self.content = SKILL_MD.read_text()

    def test_skill_md_exists(self):
        self.assertTrue(SKILL_MD.exists(), "skills/email-find/SKILL.md must exist")

    def test_has_all_five_phases(self):
        for i in range(1, 6):
            self.assertIn(f"Phase {i}", self.content, f"Phase {i} must be documented")

    def test_broad_before_narrow_rule(self):
        self.assertIn("Broad before narrow", self.content)

    def test_never_conclude_not_found_early(self):
        self.assertIn("not found", self.content.lower())
        self.assertIn("Phases 1–4", self.content)

    def test_documents_partner_domain_memory(self):
        self.assertIn("partner-domain", self.content.lower())
        self.assertIn("SUTANDO_MEMORY_DIR", self.content)

    def test_documents_full_content_refetch(self):
        self.assertIn("FULL_CONTENT", self.content)

    def test_user_invocable_frontmatter(self):
        self.assertIn("user-invocable: true", self.content)

    def test_email_find_referenced_in_built_in_tools(self):
        self.assertTrue(BUILT_IN_TOOLS.exists(), "docs/built-in-tools.md must exist")
        tools_content = BUILT_IN_TOOLS.read_text()
        self.assertIn("email-find", tools_content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
