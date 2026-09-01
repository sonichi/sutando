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
    DIVIDER_OR_DONE_RE, active_region, mask_html_comments, mask_markup)

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
    # --- Recovered from an abandoned local branch (2026-07-29) that fixed the same
    #     dashboard bug and was never pushed. The helper already handles all four
    #     correctly; none of them was covered, so the behavior was right by
    #     construction and unguarded. The cases were the salvageable part.
    "divider indented with two spaces":
        ("## q1\n\nx\n\n#  Resolved\n\n## old1\n\nx\n", 1),
    "divider separated by a tab":
        ("## q1\n\nx\n\n#\tResolved\n\n## old1\n\nx\n", 1),
    "'# ResolvedIssues' is NOT the divider (word boundary)":
        ("## q1\n\nx\n\n# ResolvedIssues\n\nx\n\n## q2\n\nx\n", 2),
    "prose QUOTING the delimiter must not truncate":
        ("## q1\n\nsee the `# Resolved` divider\n\n## q2\n\nx\n\n# Resolved\n\n## old1\n\nx\n", 2),
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
#     SCOPE, not just pattern: a flat src/*.py glob would not see a divider parser
#     added under src/<subdir>/, scripts/ or skills/. Nothing lives there today
#     (verified repo-wide across 347 .py/.ts/.sh files, with a known-positive control
#     that the query does find dashboard.py:126 when reinstated) — so this widening
#     keeps that true by construction rather than by luck.
offenders, scanned = [], 0
roots = [REPO / "src", REPO / "scripts", REPO / "skills"]
for py in sorted(f for r in roots if r.is_dir() for f in r.rglob("*.py")):
    if py.name == "pending_questions_md.py" or "node_modules" in py.parts:
        continue
    scanned += 1
    for i, line in enumerate(py.read_text().splitlines(), 1):
        # Match the PROPERTY — any local divider location — not one spelling of it.
        # The first version required a REGEX literal and so missed
        # dashboard.py's `content.partition('\n# Resolved')`: a string method doing
        # the same job, on the public /json surface. That is the substring-vs-structure
        # error this very change is about, committed inside the guard against it.
        if re.search(r'(?:Resolved|Done)', line) and (
                re.search(r'r[\'"]\^#', line)
                or re.search(r'\.(?:partition|split|find|index)\(', line)):
            offenders.append(f"{py.name}:{i}")
check(f"no second divider definition in src/, scripts/ or skills/ (scanned {scanned} files; found {offenders or 'none'})",
      scanned > 20 and not offenders)

# ---------------------------------------------------------------------------
# Case F — QUOTED DIVIDERS: a fenced example, and a code span that line-wrapped.
#
# Reported on #2419 at exact head aa6a64aa (john-the-dev, P1) and corroborated the
# same hour on a third host by sonichi: the notifier dropped from 6 pending to 2
# with no owner activity, because a sentence explaining the checker wrapped so that
# `` # Resolved` `` landed line-initial inside an inline code span.
#
# Both are the SAME class as the case-A HTML comment: documentation of the divider,
# quoted. Neither was masked, so the active region collapsed and every later owner
# question vanished — from all five readers at once, since they now share this helper.
# ---------------------------------------------------------------------------
QUESTIONS = "\n\n## real one\nbody\n\n## real two\nbody\n\n# Resolved\n\n## archived\n"

check("F1 ``` fence quoting the divider does not truncate",
      n_sections("Docs:\n```md\n# Resolved\n```" + QUESTIONS) == 2)
check("F2 ~~~ fence quoting the divider does not truncate",
      n_sections("Docs:\n~~~md\n# Resolved\n~~~" + QUESTIONS) == 2)
check("F3 inline code span wrapping to column 0 does not truncate (live on host C)",
      n_sections("Docs: reads only text above the `\n# Resolved` content and counts.\n"
                 + QUESTIONS) == 2)

# Parser boundaries. These are the controls: each asserts the masker is NOT simply
# blanking anything that looks fence-ish, which would silently over-count instead.
check("F4 a shorter inner run must not close a longer fence",
      n_sections("````\n```\n# Resolved\n````" + QUESTIONS) == 2)
check("F5 a closer must use the same marker character",
      n_sections("```\n~~~\n# Resolved\n```" + QUESTIONS) == 2)
check("F6 four-space indent is an indented code block, NOT a fence (divider still real)",
      n_sections("    ```\n# Resolved\n" + QUESTIONS.lstrip("\n")) == 0)
# F7 EXPECTATION REVISED 2026-07-31 (was `== 0`). `` ``` a`b `` is not a valid FENCE
# opener (a backtick fence's info string may not contain a backtick) — that half still
# holds. But the col-0 three-run and the three-run two lines later are equal-length
# maximal runs, so they legitimately delimit a CODE SPAN around `# Resolved`. The old
# `0` encoded fence-only semantics, from before spans were masked at all. Masking is
# also the safe direction here: it over-counts (visible) rather than truncating to a
# silent zero.
check("F7 backtick-in-info is not a fence, but IS a code span (equal maximal runs)",
      n_sections("``` a`b\n# Resolved\n```" + QUESTIONS) == 2)

# ---------------------------------------------------------------------------
# Case G — UNEQUAL BACKTICK RUNS. Second P1 on #2419 (john-the-dev, head 297bd669).
# The first fix used ``(`+)(?:(?!\1).)*?\1``, which can backtrack to a PREFIX of a
# longer run and close on a PREFIX of another, pairing runs of unequal length. Markdown
# requires both delimiters to be MAXIMAL runs of EXACTLY equal length.
# ---------------------------------------------------------------------------
check("G1 unequal runs: 3-opener is not closed by a 4-run (reviewer's exact repro)",
      n_sections("Docs: ```quoted\n````\n````\n# Resolved\n```" + QUESTIONS) == 2)
check("G2 control: an exact-length pair still masks",
      n_sections("Docs: ```\n# Resolved\n```" + QUESTIONS) == 2)
check("G3 control: ordinary prose with no backticks leaves the divider real",
      n_sections("Docs: plain text.\n" + QUESTIONS.lstrip("\n")) == 2)
check("G4 control: an opener with no equal-length partner is literal, divider real",
      n_sections("Docs: ``` unclosed\n# Resolved\n" + QUESTIONS.lstrip("\n")) == 0)
check("G5 order is load-bearing: a span closer at col 0 must not open a fence",
      n_sections("Docs: above the `\n# Resolved` content.\n" + QUESTIONS) == 2)
check("F8 an UNCLOSED fence masks to EOF — over-counts, never returns a silent zero",
      n_sections("```\n# Resolved\n## a\n\n## b\n") == 2)

# ---------------------------------------------------------------------------
# Case H — CLOSER INDENTATION IS MEASURED ON THE RAW LINE. Third P1 on #2419
# (john-the-dev, head cabd2c59), mechanism confirmed by sonichi.
# `line.strip()` removes a leading TAB, and the separate `lstrip(' ')` count
# then reported an indent of 0 — two checks disagreeing about what
# "indentation" means. A tab-indented marker was accepted as a valid closer, so
# the fence closed early and a quoted `# Resolved` was exposed as the real
# divider: a silent zero on every pending-question surface.
#
# SCOPE, stated so the next reader is not misled: this closes the TILDE half.
# `~~~` can never be inline code, so nothing competes to consume the delimiter.
# The backtick cases (a run that is BOTH a plausible span delimiter and a
# plausible fence marker) are a genuine precedence conflict that no
# whole-document masking order resolves — measured across four designs — and
# remain open on this PR.
# ---------------------------------------------------------------------------
check("H1 tilde: a TAB-indented closer does not close the fence",
      n_sections("~~~\n\t~~~\n# Resolved\n~~~" + QUESTIONS) == 2)
check("H2 tilde: a SPACE+TAB-indented closer does not close the fence",
      n_sections("~~~\n \t~~~\n# Resolved\n~~~" + QUESTIONS) == 2)
check("H3 tilde control: a 4-space closer does not close (indented code block)",
      n_sections("~~~\n    ~~~\n# Resolved\n~~~" + QUESTIONS) == 2)
check("H4 tilde control: a 3-space closer IS valid, divider stays real",
      n_sections("~~~\n   ~~~\n# Resolved" + QUESTIONS) == 0)
check("H5 tilde control: a 0-space closer IS valid, divider stays real",
      n_sections("~~~\n~~~\n# Resolved" + QUESTIONS) == 0)

# ---------------------------------------------------------------------------
# Case I — BACKTICK AMBIGUITY, the half no precedence ordering could resolve.
#
# A backtick run can be a fence marker OR a span delimiter, and the two
# requirements pull opposite ways: G1 needs a span to reach ACROSS a fence,
# while these need the fence to SURVIVE an invalid closer. Five whole-document
# orderings were measured (spans-first, fences-first, fence-regions, union,
# candidate-containment) and each satisfied one by breaking the other.
#
# The resolution is not an ordering. Runs are PARTITIONED by whether each one
# could be a fence marker at all: a run with TEXT BEFORE IT, a run shorter than
# 3, or a backtick in its info string can only ever be an inline delimiter, so it
# is resolved first and may cross fences. Every other run is left to the fence
# parser and its raw 0-3-space closer contract. The two classes never compete.
#
# NOTE a tab- or 4+-space-indented run alone on its line is deliberately NOT in
# the inline class, even though it cannot open a fence either. It is an INVALID
# FENCE MARKER — ordinary text inside the enclosing fence. Treating it as inline
# let it pair with the fence's real closer, so the fence ran to EOF and swallowed
# the REAL divider too. See `_opens_span`.
# ---------------------------------------------------------------------------
check("I1 backtick: a TAB-indented closer does not close the fence",
      n_sections("```\n\t```\n# Resolved\n```" + QUESTIONS) == 2)
check("I2 backtick: a 4-space-indented closer does not close the fence",
      n_sections("```\n    ```\n# Resolved\n```" + QUESTIONS) == 2)
check("I3 backtick: an equal-length run with TRAILING TEXT is not a closer",
      n_sections("````\n```` x\n# Resolved\n````" + QUESTIONS) == 2)
check("I4 control: a properly closed backtick fence leaves an OUTSIDE divider real",
      n_sections("```\ncode\n```\n# Resolved" + QUESTIONS) == 0)
check("I5 control: a fence-eligible run does not open a span across the document",
      n_sections("```\ncode\n```\n## a\n\n## b\n# Resolved\n") == 2)

# I6 exercises the `length < 3` arm of `_opens_span`: a SHORT run that begins its
# line. Every other short-run fixture in this file has text before the backtick,
# so it returns on the text-before-it arm and this branch was never reached —
# the coverage gate is what surfaced that, not a failing assertion.
check("I6 a line-initial 1-backtick run is inline, not a fence",
      n_sections("`code`\n# Resolved" + QUESTIONS) == 0)
check("I7 a line-initial short run still pairs across lines as a span",
      n_sections("`\n# Resolved`\n" + QUESTIONS.lstrip("\n")) == 2)

# Offset invariant: every masker must preserve length AND line count, because
# agent-api derives question identity by slicing the ORIGINAL at a masked offset.
_offset_ok = True
for _doc in ("Docs:\n```md\n# Resolved\n```" + QUESTIONS,
             "Docs: above the `\n# Resolved` content.\n" + QUESTIONS,
             BANNER + QUESTIONS):
    _m = mask_markup(_doc)
    _offset_ok &= (len(_m) == len(_doc) and _m.count("\n") == _doc.count("\n"))
check("F9 masking preserves length and line count (offsets stay sliceable)", _offset_ok)

passed = sum(ok for _, ok in CASES)
for name, ok in CASES:
    print(("  ok   " if ok else "  FAIL ") + name)
print(f"\n{passed}/{len(CASES)} passed")
sys.exit(0 if passed == len(CASES) else 1)
