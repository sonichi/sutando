"""The import-claude-context skill ships every prompt its SKILL.md names.

Regression: prompts/session.md was silently swallowed by the repo's
`SESSION.md` .gitignore rule (git ignores case-insensitively on macOS), so
the shipped skill had no per-session prompt and every install improvised a
schema. The file is now prompts/session-summary.md; this test pins that each
referenced prompt exists AND is not ignored, so a rename can't regress it.
"""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "import-claude-context"


class PromptsShipped(unittest.TestCase):
    def referenced(self):
        text = (SKILL / "SKILL.md").read_text()
        names = sorted(set(re.findall(r"`prompts/([A-Za-z0-9._-]+\.md)`", text)))
        self.assertTrue(names, "SKILL.md names no prompts")
        return names

    def test_every_referenced_prompt_exists(self):
        for name in self.referenced():
            self.assertTrue((SKILL / "prompts" / name).is_file(), name)

    def test_session_summary_prompt_is_referenced(self):
        self.assertIn("session-summary.md", self.referenced())

    def test_no_prompt_is_gitignored(self):
        for name in self.referenced():
            rel = f"skills/import-claude-context/prompts/{name}"
            r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 1, f"{rel} is ignored by .gitignore")


if __name__ == "__main__":
    unittest.main()
