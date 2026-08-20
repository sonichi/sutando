#!/usr/bin/env python3
"""Repo docs that tell an agent to read a memory file that does not exist.

`CLAUDE.md` loads into every session, so a bullet naming `memory/<name>.md` is
an instruction rather than a footnote. When the file is absent the reference
silently no-ops — nothing errors, no test fails, and the agent simply never
gets the rule it was told to load. Found live on `origin/main`:
`skills/meeting-scheduler/SKILL.md` cites `memory/reference_identity_map.md`,
which exists in neither corpus on this fleet.

Run: python3 tests/health-check-memory-citations.test.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)


class MemoryCitationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hc-cites-"))
        self._repo, self._mem = hc.REPO_DIR, hc.MEMORY_DIR
        self.repo = self.tmp / "repo"
        (self.repo / "skills" / "demo").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        self.mem = self.tmp / "memory"
        self.mem.mkdir()
        hc.REPO_DIR, hc.MEMORY_DIR = self.repo, self.mem

    def tearDown(self):
        hc.REPO_DIR, hc.MEMORY_DIR = self._repo, self._mem
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cite(self, rel: str, name: str):
        (self.repo / rel).write_text(f"- Profile: `<ws>/memory/{name}.md`\n")

    def _memory(self, name: str):
        (self.mem / f"{name}.md").write_text("---\nname: x\n---\nbody\n")

    def test_a_citation_that_resolves_is_ok(self):
        self._cite("CLAUDE.md", "user_profile")
        self._memory("user_profile")
        out = hc.check_memory_citations()
        self.assertEqual(out["status"], "ok")

    def test_a_citation_to_nothing_warns_and_names_both_sides(self):
        self._cite("CLAUDE.md", "user_profile")
        self._memory("user_profile")
        self._cite("skills/demo/SKILL.md", "reference_identity_map")

        out = hc.check_memory_citations()

        self.assertEqual(out["status"], "warn")
        self.assertIn("reference_identity_map.md", out["detail"],
                      "the reader needs the file that does not exist")
        self.assertIn("skills/demo/SKILL.md", out["detail"],
                      "and the doc that told them to read it")

    def test_test_fixtures_are_not_flagged(self):
        """`tests/` memory paths are synthetic and SHOULD NOT exist.

        Flagging them would bury the one real finding under a dozen fixtures,
        which is how a warning stops being read.
        """
        self._cite("CLAUDE.md", "user_profile")
        self._memory("user_profile")
        (self.repo / "tests" / "sync.test.sh").write_text(
            "touch memory/feedback_hostA.md memory/test-1.md\n")

        out = hc.check_memory_citations()

        self.assertEqual(out["status"], "ok")
        self.assertNotIn("feedback_hostA", out["detail"])
        self.assertNotIn("test-1", out["detail"])

    def test_the_detail_names_the_corpus_it_resolved_against(self):
        """SUTANDO_MEMORY_DIR can point this at a corpus the session never loads.

        A verdict about the wrong corpus is worse than no verdict, so the path
        is stated rather than implied.
        """
        self._cite("CLAUDE.md", "user_profile")
        self._memory("user_profile")
        out = hc.check_memory_citations()
        self.assertIn(str(self.mem), out["detail"])

    def test_no_corpus_means_no_opinion(self):
        """A host without a memory dir has nothing to resolve against."""
        self._cite("CLAUDE.md", "anything")
        hc.MEMORY_DIR = self.tmp / "absent"
        self.assertIsNone(hc.check_memory_citations())

    def test_no_citations_means_no_opinion(self):
        self.assertIsNone(hc.check_memory_citations())


if __name__ == "__main__":
    unittest.main(verbosity=2)
