#!/usr/bin/env python3
"""A test that execs a bridge must compile with the module's real filename, or
coverage attributes nothing to it and the gate silently undercounts."""

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"

# `exec(src, ns)` gives every code object co_filename '<string>', so the tracer
# cannot map an executed line back to src/<bridge>.py. Compiling fixes it.
BLIND = re.compile(r"exec\(\s*src\s*,")
COMPILED = re.compile(r"exec\(\s*compile\(")

# The lint's own fixture embeds the blind form as DATA to assert against; it is
# not a loader, so exempt it by exact path rather than by pattern.
EXEMPT = {"lint-hermetic-bridge-tests.test.py"}

fails = []
scanned = 0
for path in sorted(TESTS.glob("*.test.py")):
    if path.name == pathlib.Path(__file__).name or path.name in EXEMPT:
        continue
    text = path.read_text(errors="replace")
    if "bridge.__dict__" not in text:
        continue
    scanned += 1
    if BLIND.search(text) and not COMPILED.search(text):
        fails.append(
            f"{path.name}: exec(src, ...) without compile() — coverage cannot "
            f"attribute executed lines to the bridge module"
        )

print(f"  scanned {scanned} bridge-exec test file(s)")
if fails:
    print(f"FAILED {len(fails)}:")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("all bridge-exec tests compile with the module's real filename")
