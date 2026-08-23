#!/usr/bin/env python3
"""The D1 census must stay anchored to the tree it describes, and the
committed doc must match what the census data generates (no hand edits).

Run: python3 tests/census-d1-anchors.test.py   (stdlib only)
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "census-d1.py"
DOC = REPO / "docs" / "census" / "d1-identity-census.md"


class CensusAnchors(unittest.TestCase):
    def test_every_anchor_matches_the_tree(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "--verify"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_committed_doc_is_generated_not_hand_edited(self):
        committed = DOC.read_text()
        p = subprocess.run([sys.executable, str(SCRIPT), "--write-doc"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        regenerated = DOC.read_text()
        if regenerated != committed:
            DOC.write_text(committed)  # leave the tree as we found it
        self.assertEqual(regenerated, committed,
                         "docs/census/d1-identity-census.md drifted from "
                         "scripts/census-d1.py — regenerate with --write-doc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
