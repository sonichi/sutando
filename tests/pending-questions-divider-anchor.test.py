#!/usr/bin/env python3
"""The `# Resolved` divider anchor must not match its own documentation.

REAL OUTAGE, 2026-07-30. `pending-questions.md` opens with an HTML-comment
banner warning writers not to append at EOF, and that banner *quotes the rule
it documents*:

    `check-pending-questions.py` truncates at the first `
    # Resolved` heading
    (src/check-pending-questions.py:95) and only counts sections ABOVE it.

The anchor was `r'^#\\s+Resolved\\b'`, and `\\b` is satisfied by the backtick
that follows. So the split fired on line 23 — inside the banner — the "active
region" collapsed to the banner itself, and all 44 real sections landed
"below the divider" and were counted as resolved. Measured on the live host:
`get_waiting_questions()` returned **0** while 43 questions were open.

The file's own warning about the divider is what disarmed the divider. A
self-documenting file necessarily contains its delimiter twice, so the consumer
must anchor on a delimiter only a REAL divider can satisfy: end-of-line.

Why this went unnoticed for ~11h: `main()` opens with `if not questions: return`
— a SILENT return. The cooldown and presenter-mode branches both print a
diagnostic; the zero branch prints nothing, so a broken parse is indistinguishable
from a quiet day. Worse, `deliver()` already carries an `undrained_proactive_files()`
detector built to catch exactly this class of "claimed an outcome it never
achieved" — but it sits DOWNSTREAM of that early return, so the guard was
unreachable via the bug that mattered.

Failure direction of the fix: a legitimate divider written `# Resolved (archive)`
is no longer matched, so resolved entries get counted as open — the owner is
over-notified. Loud, and self-correcting. The old behavior failed silent.

Run: python3 tests/pending-questions-divider-anchor.test.py
"""
import importlib.util
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

OLD_ANCHOR = r'^#\s+Resolved\b'          # the buggy one, for the control
CASES = []


def check(name, got, want=True):
    CASES.append((name, bool(got) is want))


# The live banner, reduced to the part that matters: a line that begins with
# "# Resolved" and continues. Reproduced verbatim in shape from the real file.
FIXTURE = """<!-- ============================================================================
     ⚠ WRITERS READ THIS FIRST — do NOT append new questions to the END.
     `check-pending-questions.py` truncates at the first `
# Resolved` heading
     (src/check-pending-questions.py:95) and only counts sections ABOVE it.
============================================================================ -->

## Open question one

Some prose with no Status marker, which counts as unanswered by convention.

## Open question two

More prose.

# Resolved

## An old answered thing

Prose.
"""

# --- PREMISE: the fixture must actually contain the decoy, else every assertion
#     below is vacuous (it would just be testing an ordinary file).
decoy = [i for i, l in enumerate(FIXTURE.splitlines(), 1)
         if re.match(OLD_ANCHOR, l) and l.strip() != "# Resolved"]
real = [i for i, l in enumerate(FIXTURE.splitlines(), 1) if l.strip() == "# Resolved"]
assert decoy, "PREMISE FAILED: fixture has no in-prose '# Resolved' decoy — test is vacuous"
assert real, "PREMISE FAILED: fixture has no real '# Resolved' divider — test is vacuous"
assert decoy[0] < real[0], "PREMISE FAILED: decoy must precede the real divider"
print(f"premise ok — decoy at line {decoy[0]}, real divider at line {real[0]}\n")

# --- CONTROL (known-positive): the OLD anchor must get this WRONG. Without this,
#     a green suite proves nothing — the fixture might not reproduce the bug.
old_region = re.split(OLD_ANCHOR, FIXTURE, maxsplit=1, flags=re.MULTILINE)[0]
old_count = len(re.findall(r'^## ', old_region, re.MULTILINE))
check(f"CONTROL: old anchor truncates at the banner (found {old_count} sections, expected 0)",
      old_count == 0)

# --- The shipped predicate, pointed at the fixture.
spec = importlib.util.spec_from_file_location(
    "cpq", REPO / "src" / "check-pending-questions.py")
cpq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpq)

with tempfile.TemporaryDirectory() as td:
    pq = pathlib.Path(td) / "pending-questions.md"
    pq.write_text(FIXTURE)
    cpq.PQ_FILE = pq
    got = cpq.get_waiting_questions()

titles = [q.get("title", "") for q in got]
check(f"shipped predicate sees both open questions (got {len(got)})", len(got) == 2)
check("the resolved section below the real divider is excluded",
      not any("old answered" in t.lower() for t in titles))
check("both open titles are present",
      any("one" in t.lower() for t in titles) and any("two" in t.lower() for t in titles))

# --- A file with NO divider at all must still work (the anchor is a no-op there).
with tempfile.TemporaryDirectory() as td:
    pq = pathlib.Path(td) / "pending-questions.md"
    pq.write_text("## Only question\n\nProse.\n")
    cpq.PQ_FILE = pq
    check("no-divider file still counts its sections", len(cpq.get_waiting_questions()) == 1)

# --- RATCHET: no consumer may reintroduce the loose anchor. Four independent
#     copies existed (agent-api, morning-briefing, check-pending-questions,
#     friction-detector); a fifth would silently go dark the same way.
loose = []
scanned = 0
for py in sorted((REPO / "src").glob("*.py")):
    scanned += 1
    for n, line in enumerate(py.read_text().splitlines(), 1):
        if re.search(r'(?:Resolved|Done\))\\b', line):
            loose.append(f"{py.name}:{n}")
# Report SCOPE alongside the result: a zero from a scan that read nothing is
# not a pass.
check(f"no loose divider anchor in src/ (scanned {scanned} files; found {loose or 'none'})",
      scanned > 20 and not loose)

passed = sum(ok for _, ok in CASES)
for name, ok in CASES:
    print(("  ok   " if ok else "  FAIL ") + name)
print(f"\n{passed}/{len(CASES)} passed")
sys.exit(0 if passed == len(CASES) else 1)
