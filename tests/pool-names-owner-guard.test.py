#!/usr/bin/env python3
"""No seam formats a worker name itself — src/pool_names.py is the one owner.

Greps src/ and scripts/ (Python + shell) for `core-{`, `worker-{`, a bare
`"core-"`/`"worker-"` literal, or a shell `core-$`/`worker-$` builder outside
the owner. A hit is a second copy of the mapping, which is how one spelling
drifts from the other.

Run: python3 tests/pool-names-owner-guard.test.py
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OWNER = REPO / "src" / "pool_names.py"
BUILDER = re.compile(
    r"""core-\{|worker-\{|["']core-["']|["']worker-["']|core-\$|worker-\$""")


def _candidates():
    for top in ("src", "scripts"):
        for p in sorted((REPO / top).rglob("*")):
            if p.suffix in (".py", ".sh") and p.is_file() and p != OWNER:
                yield p


class OwnerGuardTest(unittest.TestCase):
    def test_no_stray_worker_name_builders(self):
        hits = []
        for p in _candidates():
            try:
                lines = p.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines, 1):
                if BUILDER.search(line):
                    hits.append(f"{p.relative_to(REPO)}:{i}: {line.strip()}")
        self.assertEqual(hits, [], "worker-name builders outside pool_names.py:\n"
                         + "\n".join(hits))

    def test_owner_exists_and_is_stdlib_only(self):
        body = OWNER.read_text()
        imports = re.findall(r"^(?:import\s+([\w.]+)\s*$|from\s+([\w.]+)\s+import)", body, re.M)
        imports = [a or b for a, b in imports]
        self.assertTrue(imports)
        self.assertTrue(set(imports) <= {"os", "re", "sys", "__future__"}, imports)


if __name__ == "__main__":
    unittest.main(verbosity=2)
