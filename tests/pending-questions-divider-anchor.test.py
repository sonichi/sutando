#!/usr/bin/env python3
"""Locating the `# Resolved` divider — one definition, and it must survive both hosts.

REAL OUTAGE, 2026-07-30, in two opposite directions from the SAME loose anchor
(`r'^#\\s+Resolved\\b'`, independently copied into four readers):

  * **under-count.** `pending-questions.md` opens with a banner warning writers not
    to append at EOF, and it *quotes the rule it documents*, putting
    `` # Resolved` heading `` line-initial inside an HTML comment. `\\b` is satisfied
    by the backtick, so the split fired in the banner, the active region collapsed to
    the banner, and all 89 real sections were counted as resolved. Measured on the
    live host: **0 open while 43 were open**, for ~11h.
  * **over-count.** On a host whose file has no clean divider, the split is a no-op
    and the audit trail is counted as pending. Measured: **101**.

The first fix anchored the divider to end-of-line (`^#[ \\t]+Resolved[ \\t\\r]*$`).
It killed the under-count and **introduced a regression**: `# Resolved (archive)` is a
legitimate divider that stops matching, so its entire audit trail is counted as open.
And because a stricter anchor can only make the active region LARGER, it can never
fix an over-count either — that direction was never addressed. Case B below is the
guard for exactly that; it is the case a green suite would otherwise have hidden.

So the discriminator is not how the divider ENDS — it is whether the match sits inside
a comment. A real divider never does; documentation of the divider always does.
`active_region` masks comment bodies (preserving length and line count, so offsets stay
valid for callers that slice) and then matches a suffix-permissive anchor.

Run: python3 tests/pending-questions-divider-anchor.test.py
"""
import importlib.util
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from pending_questions_md import (  # noqa: E402
    DIVIDER_OR_DONE_RE, active_region, mask_html_comments)

OLD_ANCHOR = r'^#\s+Resolved\b'              # the original bug
EOL_ANCHOR = r'^#[ \t]+Resolved[ \t\r]*$'    # the first fix, which regressed case B
CASES = []


def check(name, got, want=True):
    CASES.append((name, bool(got) is want))


def n_sections(text, divider=None):
    region = active_region(text, divider) if divider else active_region(text)
    return len(re.findall(r'^## ', region, re.MULTILINE))


BANNER = ("<!-- =====\n"
          "  `check-pending-questions.py` truncates at the first `\n"
          "# Resolved` heading\n"
          "  and only counts sections ABOVE it.\n"
          "===== -->\n")

FIXTURES = {
    # name                              text                                     want
    "no divider at all (over-count host)":
        ("# Pending owner decisions\n\n## q1\n\nx\n\n## q2\n\nx\n", 2),
    "suffixed divider '# Resolved (archive)'  <-- EOL-anchor regression guard":
        ("## q1\n\nx\n\n# Resolved (archive)\n\n## old1\n\nx\n\n## old2\n\nx\n", 1),
    "clean divider":
        ("## q1\n\nx\n\n# Resolved\n\n## old1\n\nx\n", 1),
    "decoy inside HTML comment (the live outage)":
        (BANNER + "\n## q1\n\nx\n\n# Resolved\n\n## old1\n\nx\n", 1),
    "bare '#' then 'Resolved' on the next line must NOT match":
        ("## q1\n\nx\n#\n\nResolved discussion\n\n## q2\n\nx\n", 2),
    "CRLF divider":
        ("## q1\n\nx\n\n# Resolved\r\n\n## old1\n\nx\n", 1),
}

# --- PREMISE: the decoy fixture must actually contain a line-initial in-comment
#     decoy that PRECEDES the real divider, else that case is vacuous.
decoy_text = FIXTURES["decoy inside HTML comment (the live outage)"][0]
lines = decoy_text.splitlines()
decoy = [i for i, l in enumerate(lines, 1) if re.match(OLD_ANCHOR, l) and l.strip() != "# Resolved"]
real = [i for i, l in enumerate(lines, 1) if l.strip() == "# Resolved"]
assert decoy and real and decoy[0] < real[0], (
    "PREMISE FAILED: decoy fixture must hold an in-comment decoy before the real divider")
assert mask_html_comments(decoy_text).count("# Resolved") == 1, (
    "PREMISE FAILED: masking must hide the decoy and keep the real divider")
print(f"premise ok — decoy line {decoy[0]}, real divider line {real[0]}\n")

# --- CONTROLS (known-positive): both PRIOR implementations must get a case wrong,
#     otherwise these fixtures do not reproduce the bugs they claim to guard.
def sections_with(pat, text):
    return len(re.findall(r'^## ', re.split(pat, text, maxsplit=1, flags=re.MULTILINE)[0],
                          re.MULTILINE))

check("CONTROL: the ORIGINAL anchor under-counts the decoy file (0, not 1)",
      sections_with(OLD_ANCHOR, decoy_text) == 0)
suffixed = FIXTURES["suffixed divider '# Resolved (archive)'  <-- EOL-anchor regression guard"][0]
check("CONTROL: the EOL anchor over-counts a suffixed divider (3, not 1)",
      sections_with(EOL_ANCHOR, suffixed) == 3)

# --- The shipped helper on every fixture.
for name, (text, want) in FIXTURES.items():
    got = n_sections(text)
    check(f"{name} -> {want}", got == want)

# friction-detector's variant treats `# Done` as an archive divider too.
check("'# Done' divider honored for friction-detector",
      n_sections("## q1\n\nx\n\n# Done\n\n## old1\n\nx\n", DIVIDER_OR_DONE_RE) == 1)

# --- Masking must preserve offsets, or agent-api's slice-by-offset silently shifts
#     and question identity (derived from section bodies) changes.
sample = BANNER + "\n## q1\n\nbody\n"
masked = mask_html_comments(sample)
check("masking preserves length (offsets stay valid for slicing)", len(masked) == len(sample))
check("masking preserves line count", masked.count("\n") == sample.count("\n"))

# --- The shipped predicate end-to-end, through the real notifier.
spec = importlib.util.spec_from_file_location(
    "cpq", REPO / "src" / "check-pending-questions.py")
cpq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpq)
with tempfile.TemporaryDirectory() as td:
    pq = pathlib.Path(td) / "pending-questions.md"
    pq.write_text(BANNER + "\n## Open one\n\nprose\n\n## Open two\n\nprose\n\n"
                           "# Resolved\n\n## Old\n\nprose\n")
    cpq.PQ_FILE = pq
    got = cpq.get_waiting_questions()
titles = [q.get("title", "") for q in got]
check(f"notifier sees both open questions through the banner (got {len(got)})", len(got) == 2)
check("resolved section excluded", not any("old" in t.lower() for t in titles))

# --- RATCHET: exactly ONE definition of the divider. Four independent copies is what
#     produced this outage; a fifth would go dark the same way.
offenders, scanned = [], 0
for py in sorted((REPO / "src").glob("*.py")):
    if py.name == "pending_questions_md.py":
        continue
    scanned += 1
    for i, line in enumerate(py.read_text().splitlines(), 1):
        if re.search(r'(?:Resolved|Done)\b[^\n]*', line) and re.search(r'r[\'"]\^#', line):
            offenders.append(f"{py.name}:{i}")
check(f"no second divider definition in src/ (scanned {scanned} files; found {offenders or 'none'})",
      scanned > 20 and not offenders)

passed = sum(ok for _, ok in CASES)
for name, ok in CASES:
    print(("  ok   " if ok else "  FAIL ") + name)
print(f"\n{passed}/{len(CASES)} passed")
sys.exit(0 if passed == len(CASES) else 1)
