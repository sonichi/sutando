#!/usr/bin/env python3
"""All five pending-questions readers must agree on where the archive begins.

The 2026-07-30 outage was not one bug in one place: `r'^#\\s+Resolved\\b'` had been
independently copied into four readers — the notifier, morning-briefing, agent-api
(dashboard) and friction-detector — all resolving the same
`personal_path("pending-questions.md")`. One defect therefore went dark in four
places at once, and nothing in the tree would have noticed if only three had been
fixed.

They now share `src/pending_questions_md.active_region`. This test is the guard on
that sharing: it feeds ONE fixture — carrying the real decoy shape, a
`` # Resolved` heading `` line inside the banner's HTML comment — to every reader and
asserts they all draw the same line between open and archived.

A reader that reintroduces its own divider logic keeps passing its own unit tests and
fails here, which is the point.

Run: python3 tests/pending-questions-readers-agree.test.py
"""
import importlib.util
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# The decoy is line-initial INSIDE the comment, exactly as the live file has it.
FIXTURE = """<!-- =====================================================================
     ⚠ WRITERS READ THIS FIRST — do NOT append new questions to the END.
     `check-pending-questions.py` truncates at the first `
# Resolved` heading
     and only counts sections ABOVE it.
     ===================================================================== -->

## OPEN-ALPHA — a question that is still waiting

Prose body with no Status marker, which is unanswered by convention.

## OPEN-BETA — a second waiting question

**Status:** unanswered

More prose.

# Resolved

## ARCHIVED-GAMMA — answered last week

**Status:** resolved

Prose.
"""

OPEN = ("OPEN-ALPHA", "OPEN-BETA")
ARCHIVED = "ARCHIVED-GAMMA"

CASES = []


def check(name, got, want=True):
    CASES.append((name, bool(got) is want))


def blob(items):
    """Readers return different shapes (`title` vs `text` vs plain strings);
    compare on the flattened content so this guard is about the CUT, not the schema."""
    return " ".join(str(i) for i in items)


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"_m_{name.replace('-', '_')}",
                                                  REPO / "src" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tmpdir = tempfile.TemporaryDirectory()
pq_path = pathlib.Path(tmpdir.name) / "pending-questions.md"
pq_path.write_text(FIXTURE)

# --- PREMISE: the fixture must actually carry the decoy before the real divider,
#     or every assertion below is just testing an ordinary file.
lines = FIXTURE.splitlines()
decoy = [i for i, l in enumerate(lines, 1) if l.startswith("# Resolved") and l.strip() != "# Resolved"]
real = [i for i, l in enumerate(lines, 1) if l.strip() == "# Resolved"]
assert decoy and real and decoy[0] < real[0], (
    "PREMISE FAILED: fixture must hold an in-comment decoy preceding the real divider")
print(f"premise ok — decoy line {decoy[0]}, real divider line {real[0]}\n")

results = {}

# 1. the notifier — reads PQ_FILE
cpq = load("check-pending-questions")
cpq.PQ_FILE = pq_path
results["check-pending-questions"] = blob(cpq.get_waiting_questions())

# 2. agent-api — takes content directly
api = load("agent-api")
results["agent-api"] = blob(api.parse_pending_questions(FIXTURE))

# 3. morning-briefing — resolves its own path
mb = load("morning-briefing")
mb.personal_path = lambda *a, **k: pq_path
results["morning-briefing"] = blob(mb.get_pending_questions())

# 4. friction-detector — resolves its own path, and also honors `# Done`
fd = load("friction-detector")
fd.personal_path = lambda *a, **k: pq_path
results["friction-detector"] = blob(fd.check_pending_questions())

# 5. dashboard — the public /json surface. Missed by the first version of this
#    guard AND by the ratchet, because it located the divider with
#    `content.partition('\n# Resolved')` — a STRING method, while the ratchet only
#    looked for a regex literal. On the decoy shape it reported open=0 done=3 where
#    the truth is 2 and 1: a confident zero on a surface users read.
dash = load("dashboard")
dash.personal_path = lambda *a, **k: pq_path
results["dashboard"] = blob([f"{k}={v}" for k, v in dash.get_pending_count().items()])

# --- Every reader must see through the banner to the open questions, and none may
#     count the archived one.
for reader, seen in results.items():
    if reader == "dashboard":
        # Counts, not titles: 2 open sections above the divider, 1 below it.
        check(f"dashboard: open=2 past the banner decoy (got {seen})", "open=2" in seen)
        check(f"dashboard: done=1, not the whole file (got {seen})", "done=1" in seen)
        continue
    check(f"{reader}: sees the open questions past the banner decoy",
          all(o in seen for o in OPEN))
    check(f"{reader}: does NOT count the archived section",
          ARCHIVED not in seen)

# --- And they must agree with each other, not merely each be self-consistent.
open_seen = {r: tuple(o for o in OPEN if o in b)
             for r, b in results.items() if r != "dashboard"}
check(f"all readers agree on the open set ({open_seen})",
      len(set(open_seen.values())) == 1)

passed = sum(ok for _, ok in CASES)
for name, ok in CASES:
    print(("  ok   " if ok else "  FAIL ") + name)
print(f"\n{passed}/{len(CASES)} passed")
tmpdir.cleanup()
sys.exit(0 if passed == len(CASES) else 1)
