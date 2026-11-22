#!/usr/bin/env python3
"""Every repo file a package test loads from outside the package reaches the publish gate.

CI skips the ag2-sparrow publish gate on a PR whose changes don't reach it
(packages/ag2-sparrow/tools/gate_reach.py: sync_from_src.MAP plus gate_inputs.txt). A package test
that loads a repo file neither covers would let a PR change that file and skip every suite that
reads it. This scans each package test for the repo paths it names: adjacent string literals that
join into an existing path (`REPO / "src" / "x.py"`), a literal that is a file name unique in the
repo, and imports that resolve only to a repo module (`tests/_helpers`, `src/`). Each must be covered.

Run: python3 tests/sparrow-gate-inputs-cover-package-loads.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = "packages/ag2-sparrow"
sys.path.insert(0, str(REPO / PKG / "tools"))
import gate_reach  # noqa: E402

TRACKED = [p for p in subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True,
                                     check=True).stdout.splitlines() if p]
OUTSIDE = [p for p in TRACKED if not p.startswith(PKG + "/") and "node_modules" not in p]
OUTSIDE_SET = set(OUTSIDE)
OUTSIDE_DIRS = {str(Path(p).parent) for p in OUTSIDE}
BASENAMES = {}
for _p in OUTSIDE:
    BASENAMES.setdefault(Path(_p).name, []).append(_p)


def _strings(tree):
    docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    return sorted((n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and id(n) not in docs), key=lambda n: (n.lineno, n.col_offset))


def repo_paths_loaded(source: str) -> set:
    """Repo paths outside the package that one test file's source names or imports."""
    tree = ast.parse(source)
    found = set()
    parts = [part for n in _strings(tree) for part in n.value.split("/") if part]
    # A repo path here is at most six components deep.
    for j in range(len(parts)):
        for i in range(max(0, j - 5), j + 1):
            cand = "/".join(parts[i:j + 1])
            # One bare component is a name, not a path (`ADAPTER / "README.md"`); names go below.
            if i < j and (cand in OUTSIDE_SET or cand in OUTSIDE_DIRS):
                found.add(cand)
    for n in _strings(tree):
        hits = BASENAMES.get(n.value, [])
        # A name unique in the repo is that file, unless it sits at the root (README.md, AGENTS.md).
        if "." in n.value and len(hits) == 1 and "/" in hits[0]:
            found.add(hits[0])
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        for name in names:
            if (REPO / PKG / "tests" / f"{name}.py").exists() or (REPO / PKG / name).exists():
                continue
            for root in ("tests", "src"):
                for cand in (f"{root}/{name}.py", f"{root}/{name}"):
                    if cand in OUTSIDE_SET or cand in OUTSIDE_DIRS:
                        found.add(cand)
    # A directory that a longer found path runs through was only a step on the way to it.
    return {p for p in found if not any(q.startswith(p + "/") for q in found)}


def _covered(path: str) -> bool:
    if gate_reach.covers(path):
        return True
    # A directory is covered when a glob covers everything under it.
    return path in OUTSIDE_DIRS and all(gate_reach.covers(p) for p in OUTSIDE if p.startswith(path + "/"))


class GateInputsCoverPackageLoads(unittest.TestCase):
    def test_every_loaded_repo_path_reaches_the_gate(self):
        missing = {}
        for f in sorted((REPO / PKG / "tests").glob("*.py")):
            for p in repo_paths_loaded(f.read_text(encoding="utf-8")):
                if not _covered(p):
                    missing.setdefault(p, []).append(f.name)
        self.assertEqual(missing, {}, "package tests load these repo paths, but a PR changing them would skip "
                         "the publish gate; add each to packages/ag2-sparrow/tools/gate_inputs.txt:\n  "
                         + "\n  ".join(f"{p}  (loaded by {', '.join(v)})" for p, v in sorted(missing.items())))

    def test_the_scan_sees_each_load_form(self):
        self.assertIn("src/remote-gateway-bridge.test.py",
                      repo_paths_loaded('REPO_SRC = R / "src"\nB = REPO_SRC / "remote-gateway-bridge.test.py"\n'))
        self.assertIn("tests/_helpers", repo_paths_loaded("from _helpers.hermetic_env import x\n"))
        self.assertIn("tests/holder-lock.test.py", repo_paths_loaded('n = "holder-lock.test.py"\n'))
        self.assertEqual(repo_paths_loaded('"""Mentions src/remote-gateway-bridge.test.py."""\nimport os\n'), set())

    def test_the_classifier_reaches_on_a_fixture_only_change_and_not_on_an_unrelated_one(self):
        self.assertTrue(gate_reach.reaches(["src/remote-gateway-bridge.test.py"]))
        self.assertTrue(gate_reach.reaches(["src/outbox.py"]), "a MAP source reaches the gate")
        self.assertTrue(gate_reach.reaches(["tests/_helpers/hermetic_env.py"]))
        self.assertTrue(gate_reach.reaches([f"{PKG}/ag2_sparrow/x.py"]))
        self.assertFalse(gate_reach.reaches(["src/voice-agent.ts", "docs/README.md"]))
        self.assertFalse(gate_reach.reaches([]))

    def test_an_uncovered_load_is_reported(self):
        self.assertFalse(_covered("src/voice-agent.ts"))


if __name__ == "__main__":
    unittest.main()
