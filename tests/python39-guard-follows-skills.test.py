#!/usr/bin/env python3
"""The 3.9 guard must follow production python that moved out of src/.

Run: python3 tests/python39-guard-follows-skills.test.py

python39-compat.yml is the ONLY job validating 3.9.6; its own header says a
3.10+ regression is otherwise "green everywhere and only fails at bridge
restart". When a module relocates from src/ into a skill, both the workflow
TRIGGER and the scanner TARGET have to follow it or the guard silently stops
covering the code it guards. Neither half was pinned, so reverting either left
every test green.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WF = REPO / ".github" / "workflows" / "python39-compat.yml"
SCANNER = REPO / "scripts" / "check-python39-compat.py"


class TestPython39GuardFollowsSkills(unittest.TestCase):
    def test_workflow_triggers_on_skill_python(self):
        text = WF.read_text()
        # Both path lists, not just the first; comments are legal inside one and
        # a run-of-entries regex stops there, reporting a present entry absent.
        blocks = re.findall(r"paths:\n((?:\s+(?:-\s'[^']+'|#[^\n]*)\n)+)", text)
        self.assertGreaterEqual(len(blocks), 2,
                                "expected pull_request AND push path lists in python39-compat.yml")
        for i, b in enumerate(blocks):
            self.assertTrue(any("skills/" in ln and ln.strip().endswith(".py'") for ln in b.splitlines()),
                            f"path list #{i+1} does not trigger on any skills/**.py — production "
                            f"python that moved into a skill would not run this job:\n{b}")

    def test_scanner_default_targets_include_skills(self):
        text = SCANNER.read_text()
        m = re.search(r"DEFAULT_TARGETS\s*=\s*\(([^)]*)\)", text)
        self.assertIsNotNone(m, "DEFAULT_TARGETS not found in check-python39-compat.py")
        targets = {t.strip().strip('\'"') for t in m.group(1).split(",") if t.strip()}
        self.assertIn("skills", targets,
                      f"scanner defaults to {sorted(targets)} — a triggered run would still "
                      "scan only src/, so the job passes without reading the moved module")
        self.assertIn("src", targets, "src must remain scanned")

    def test_the_moved_module_is_actually_reachable(self):
        """The two halves above are necessary; this is the end-to-end claim."""
        moved = REPO / "skills" / "worker-pool" / "scripts"
        if not moved.is_dir():
            self.skipTest("worker-pool scripts not present on this base")
        pys = list(moved.rglob("*.py"))
        self.assertTrue(pys, "no .py under the skill's scripts/ — nothing for the guard to reach")
        m = re.search(r"DEFAULT_TARGETS\s*=\s*\(([^)]*)\)", SCANNER.read_text())
        targets = {t.strip().strip('\'"') for t in m.group(1).split(",") if t.strip()}
        self.assertTrue(any(str(p.relative_to(REPO)).startswith(t + "/") for p in pys for t in targets),
                        f"{pys[0].relative_to(REPO)} is under no scanned target {sorted(targets)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
