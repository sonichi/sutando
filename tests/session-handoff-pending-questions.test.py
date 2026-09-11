#!/usr/bin/env python3
"""session-handoff.sh must not extract pending questions with a heading-shape
pattern of its own.

Regression: the "## Pending Questions" section of session-state.md used
`grep -A1 "^## Q"`, which matches only the legacy `## Q1 — Title` heading. Real
pending-questions.md files use dated/emoji headings (`## 🔴 2026-07-25 — ...`),
so the section rendered EMPTY on every compaction while questions were waiting.

Empty is the failure that matters: it is indistinguishable from "nothing
pending", so the successor session is told the owner has no open decisions.

This had already been fixed once, for the path only (see the comment block above
the extractor). Repairing the path is what converted an honest "None" — printed
when the file was not found — into a silent "". A partial fix made the failure
quieter, which is why this test pins the *content*, not just the path.
"""
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HANDOFF = REPO / "src" / "session-handoff.sh"
PARSER = REPO / "src" / "check-pending-questions.py"

failures = []


def check(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


# The three heading shapes that have appeared in real files. The modern shape is
# the one that broke; a fixture using only the legacy shape would pass against
# the very bug this test exists to catch.
SHAPES = {
    "legacy `## Q1 — Title`": "## Q1 — Legacy shape\n\nbody text\n",
    "plain `## Title`": "## Plain heading\n\nbody text\n",
    "dated/emoji (what real files use)": "## 🔴 2026-07-25 — Dated shape\n\nbody text\n",
}

print("session-handoff pending-questions extraction")

# 1. Structural: the script must not carry its own heading-shape extractor.
src = HANDOFF.read_text()
check(
    "session-handoff.sh does not grep a legacy-only heading shape",
    not re.search(r'grep\s+-A1\s+"\^##\s*Q"', src),
    "-- found the shape-specific `grep -A1 \"^## Q\"` extractor",
)
check(
    "session-handoff.sh delegates to check-pending-questions.py",
    "check-pending-questions.py" in src,
    "-- no reference to the canonical parser",
)
check(
    "a parse failure is reported, not rendered as an empty section",
    "could not parse" in src,
    "-- no explicit parse-failure branch; empty output would read as 'nothing pending'",
)

# 2. Behavioural: the canonical parser handles every shape, and the retired grep
#    handles only one. This is what makes the delegation above load-bearing.
spec = importlib.util.spec_from_file_location("cpq", PARSER)
cpq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpq)

for label, section in SHAPES.items():
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "pending-questions.md"
        fixture.write_text("# Pending Questions\n\n" + section)
        cpq.PQ_FILE = fixture
        found = cpq.get_waiting_questions()
        check(f"canonical parser finds: {label}", len(found) >= 1, f"-- got {len(found)}")

        legacy = subprocess.run(
            ["grep", "-A1", "^## Q", str(fixture)], capture_output=True, text=True
        )
        is_legacy_shape = label.startswith("legacy")
        matched = legacy.returncode == 0
        check(
            f"retired grep {'matches' if is_legacy_shape else 'MISSES'}: {label}",
            matched == is_legacy_shape,
            "-- the retired pattern's coverage is not what the regression assumed",
        )


# 4. The guard and the read must name the SAME file: on disagreement `qs` is
# empty, prints "None", and the honest parse-failure branch never fires.
_snip = re.search(r"pq_out=\$\(python3 -c \"\n(.*?)\"\s*2>/dev/null\)",
                  src, re.S)
check("the extractor snippet is still locatable", bool(_snip))
if _snip:
    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "pending-questions.md"
        real.write_text("## UNIQUEMARKER-guarded-file question\n\nbody\n")
        code = _snip.group(1).replace("$REPO", str(REPO)).replace("$PQ_PATH", str(real))

        def run(src_code):
            return subprocess.run([sys.executable, "-c", src_code],
                                  capture_output=True, text=True).stdout

        fixed = run(code)
        # Negative control: strip the binding and the read reverts to whatever
        # the module resolved on its own — the pre-fix behaviour.
        unbound = run(re.sub(r"^m\.PQ_FILE = _P\(.*\)$", "", code, flags=re.M))

        check("the read uses the guarded file", "UNIQUEMARKER" in fixed,
              f"-- got {fixed.strip()[:60]!r}")
        check("control: without the binding it does NOT read the guarded file",
              "UNIQUEMARKER" not in unbound,
              "-- the test cannot distinguish bound from unbound")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    sys.exit(1)
print("PASS")
