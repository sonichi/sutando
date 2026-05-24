#!/usr/bin/env python3
"""Tests for skills/publish.py — generate_readme() function."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILLS_DIR))
from publish import generate_readme


class TestGenerateReadmeNoSkillMd(unittest.TestCase):
    def test_returns_empty_when_no_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_readme(Path(tmp))
        self.assertEqual(result, "")


class TestGenerateReadmeBasic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.skill_dir = Path(self._tmp.name)
        (self.skill_dir / "SKILL.md").write_text("# my-skill\n\nA skill.\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_readme_starts_with_skill_name(self):
        result = generate_readme(self.skill_dir)
        self.assertTrue(result.startswith(f"# {self.skill_dir.name}"))

    def test_readme_contains_install_section(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("## Install", result)
        self.assertIn("git clone", result)

    def test_readme_contains_symlink_with_skill_name(self):
        result = generate_readme(self.skill_dir)
        self.assertIn(self.skill_dir.name, result)

    def test_readme_contains_ai_agent_tagline(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("A Claude Code skill for AI agents", result)

    def test_readme_contains_sutando_credit(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("Sutando", result)

    def test_readme_contains_mit_license(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("MIT", result)

    def test_fallback_usage_when_no_when_to_use(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("See SKILL.md for details.", result)

    def test_zero_scripts_when_no_scripts_dir(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("0 scripts:", result)


class TestGenerateReadmeWithScripts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.skill_dir = Path(self._tmp.name)
        (self.skill_dir / "SKILL.md").write_text("# skill\n\nContent.\n")
        scripts = self.skill_dir / "scripts"
        scripts.mkdir()
        (scripts / "do-things.py").write_text("")
        (scripts / "run_all.sh").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def test_counts_scripts(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("2 scripts:", result)

    def test_lists_script_names(self):
        result = generate_readme(self.skill_dir)
        self.assertIn("do-things.py", result)
        self.assertIn("run_all.sh", result)

    def test_script_stem_formatted_with_spaces(self):
        result = generate_readme(self.skill_dir)
        # "do-things" → "do things", "run_all" → "run all"
        self.assertIn("do things", result)
        self.assertIn("run all", result)


class TestGenerateReadmeWhenToUseExtraction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.skill_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_extracts_when_to_use_section(self):
        (self.skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            # skill

            Intro.

            ## When to Use

            Use this when you need foo.

            ## Other Section

            Other content.
        """))
        result = generate_readme(self.skill_dir)
        self.assertIn("Use this when you need foo.", result)

    def test_when_to_use_stops_at_next_heading(self):
        (self.skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            # skill

            ## When to Use

            Relevant usage.

            ## Not Included

            Should not appear.
        """))
        result = generate_readme(self.skill_dir)
        self.assertNotIn("Should not appear", result)


if __name__ == "__main__":
    unittest.main()
