#!/usr/bin/env python3
"""The D1 census must stay anchored to the tree it describes, and the
committed doc must match what the census data generates (no hand edits).

Run: python3 tests/census-d1-anchors.test.py   (stdlib only)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "census-d1.py"
DOC = REPO / "docs" / "census" / "d1-identity-census.md"

PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]


class CensusAnchors(unittest.TestCase):
    def test_every_anchor_matches_the_tree(self):
        p = subprocess.run([*PYBASE, str(SCRIPT), "--verify"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_committed_doc_is_generated_not_hand_edited(self):
        committed = DOC.read_text()
        p = subprocess.run([*PYBASE, str(SCRIPT), "--write-doc"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        regenerated = DOC.read_text()
        if regenerated != committed:
            DOC.write_text(committed)  # leave the tree as we found it
        self.assertEqual(regenerated, committed,
                         "docs/census/d1-identity-census.md drifted from "
                         "scripts/census-d1.py — regenerate with --write-doc")



class AbsentAnchorMechanism(unittest.TestCase):
    def test_absent_anchor_fails_only_when_pattern_appears(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("census_d1", SCRIPT)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "mod.py").write_text("clean = True\n")
            row = [("cell", {
                "discord": ("claims none", [("mod.py", r"attempt_id", "absent")]),
                "ag2space": ("x", [("mod.py", r"clean")]),
                "slack": ("x", [("mod.py", r"clean")]),
            })]
            old_repo, old_rows = m.REPO, m.ROWS
            try:
                m.REPO, m.ROWS = Path(td), row
                self.assertEqual(m.verify(), 0)   # absent + no match = clean
                (Path(td) / "mod.py").write_text("attempt_id = 1\nclean = True\n")
                self.assertEqual(m.verify(), 1)   # the tree GAINED it = rotted
            finally:
                m.REPO, m.ROWS = old_repo, old_rows

if __name__ == "__main__":
    unittest.main(verbosity=2)
