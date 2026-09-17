#!/usr/bin/env python3
"""A zero from the notifier must never be silent, and must carry its denominator.

On 2026-07-30 a divider-anchor bug made `get_waiting_questions()` return 0 while 43
questions were open. `main()` opened with a bare `if not questions: return` — no
output — so every hourly run exited in silence for ~11 hours. Two things made that
undetectable:

  * **Silence was ambiguous.** The cooldown and presenter-mode branches both PRINT a
    diagnostic; only the zero branch did not. So "broken parse" and "quiet day" looked
    identical from the outside. I misread the daily silence as cooldown — but cooldown
    would have printed, so the silence itself ruled it out.
  * **The verdict carried no denominator.** Zero out of a 5000-line file is a
    suspicious answer, and nothing said the file was 5000 lines.

`zero_reason()` fixes both: the zero path always prints, and when the active region is
empty while the file holds sections, it says so in those words rather than reporting a
clean all-clear.

Note what this does NOT do: it cannot detect a *wrong* non-zero count, and it is not a
substitute for the parser being right (that is #2419). It only guarantees that the
specific failure shape which hid for 11 hours is loud the first time it happens.

Run: python3 tests/pending-questions-zero-is-explained.test.py
"""
import contextlib
import importlib.util
import io
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

CASES = []


def check(name, got, want=True):
    CASES.append((name, bool(got) is want))


spec = importlib.util.spec_from_file_location(
    "cpq", REPO / "src" / "check-pending-questions.py")
cpq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpq)

tmp = tempfile.TemporaryDirectory()


def reason_for(name, content=None):
    """Call zero_reason if it exists, else return "" .

    Deliberately tolerant: against a PRE-FIX check-pending-questions.py this file must
    still RUN, or the "does it fail without the fix?" control dies on AttributeError
    and proves nothing about behavior. The load-bearing assertion below is that
    `main()` prints — that one is meaningful on both versions.
    """
    p = pathlib.Path(tmp.name) / f"{name}.md"
    if content is not None:
        p.write_text(content)
    cpq.PQ_FILE = p
    fn = getattr(cpq, "zero_reason", None)
    return fn() if fn else ""


# The failure shape that hid for 11h: the file is full, the active region is empty.
PARSE_FAULT = "# Resolved\n\n## Q1\n\nprose\n\n## Q2\n\nprose\n"
QUIET_DAY = "## Q1\n\n**Status:** resolved\n\n# Resolved\n\n## Old\n\nprose\n"
EMPTY = "# Pending questions\n\nnothing filed yet\n"

fault = reason_for("fault", PARSE_FAULT)
quiet = reason_for("quiet", QUIET_DAY)
empty = reason_for("empty", EMPTY)
missing = reason_for("does-not-exist")

check("parse fault is NAMED as a probable fault", "parse fault" in fault)
check("parse fault reports how many sections the file holds", "2 '## ' section(s)" in fault)
check("parse fault warns against trusting the zero", "before trusting this zero" in fault)

check("a genuinely quiet day is NOT called a fault", "parse fault" not in quiet)
check("quiet day still reports the denominator", "of 2 '## ' section(s)" in quiet)
check("empty file is described as empty", "no sections or bullets" in empty)
check("missing file is described as missing", "no file at" in missing)

# --- REVIEW REGRESSION (raised on the first revision, with a reproduction).
#     get_waiting_questions() counts BOTH `## ` sections and free-form
#     `- **[label]**` bullets, but the first zero_reason() computed its denominator
#     from sections alone and only entered the suspicious branch when
#     `active == 0 and total > 0`. A bullet-only file with a lost active region
#     therefore printed "every one is explicitly resolved/answered" — a
#     trusted-looking zero in precisely the shape this function exists to flag.
#     The denominator must cover the same populations the numerator counts.
BULLET_ONLY_FAULT = "# Resolved\n\n- **[OPEN-ALPHA, 2026-07-30]** still waiting\n"
MIXED_FAULT = "# Resolved\n\n## Q1\n\nprose\n\n- **[OPEN-BETA, 2026-07-30]** waiting\n"

bullet_fault = reason_for("bullet_fault", BULLET_ONLY_FAULT)
mixed_fault = reason_for("mixed_fault", MIXED_FAULT)

check("bullet-only file with a lost active region is NAMED a parse fault",
      "parse fault" in bullet_fault)
check("bullet-only fault must NOT claim everything is resolved",
      "resolved/answered" not in bullet_fault)
check("bullet-only fault reports the bullet denominator",
      "1 bullet entr(ies)" in bullet_fault)
check("mixed sections+bullets with a lost active region is a parse fault",
      "parse fault" in mixed_fault)
check("mixed fault reports BOTH populations",
      "'## ' section(s)" in mixed_fault and "bullet entr(ies)" in mixed_fault)

# --- A CLEARED FILE IS NOT A FAULT. Live host: 55 archived, 0 open, called
#     "the shape of a parse fault" on every run.
CLEARED_INLINE = ("# Open\n\n\n# Resolved\n\n"
                  "## [RESOLVED 2026-08-29] shipped\n\nprose\n\n"
                  "## [DONE] answered by the owner\n\nprose\n")
CLEARED_STATUS = ("# Open\n\n\n# Resolved\n\n"
                  "## Q1\n\n**Status:** resolved\n\n"
                  "## Q2\n\n**Status:** answered\n")
cleared_inline = reason_for("cleared_inline", CLEARED_INLINE)
cleared_status = reason_for("cleared_status", CLEARED_STATUS)

check("a fully archived file is NOT called a parse fault (inline markers)",
      "parse fault" not in cleared_inline)
check("a fully archived file is NOT called a parse fault (status fields)",
      "parse fault" not in cleared_status)
check("the all-clear says the archive is what holds the entries",
      "below the archive divider" in cleared_inline)
check("the all-clear still carries the denominator",
      "2 '## ' section(s)" in cleared_inline)

# An unmarked archive entry is the NORMAL shape of a resolved one, not a stranded
# one; two hosts measured 73 and 53 false titles inferring it from absence.
UNMARKED_ARCHIVE = ("# Open\n\n\n# Resolved\n\n"
                    "## Should the digest include Q3 numbers?\n\nfree-form prose\n\n"
                    "## RESOLVED 2026-08-31 - disk item is self-answered\n\nprose\n")
unmarked = reason_for("unmarked", UNMARKED_ARCHIVE)
check("an unmarked archived section is NOT called a parse fault",
      "parse fault" not in unmarked)
check("the all-clear does not claim every entry carries a resolution marker",
      "explicitly resolved" not in unmarked)

# REAL-WORLD SHAPE. Every healthy fixture above opens with '# Open', but no real
# pending-questions.md does; both on this host start '# Pending Questions'.
REAL_HEADING = ("# Pending Questions - HOSTNAME\n\n\n# Resolved\n\n"
                "## [RESOLVED 2026-08-31] shipped\n\nprose\n")
real_heading = reason_for("real_heading", REAL_HEADING)
check("a healthy file whose heading is NOT '# Open' is not a fault",
      "parse fault" not in real_heading)
check("the real-world heading still reaches the all-clear",
      "below the archive divider" in real_heading)

# And the fault must still fire on what position CANNOT fake: no heading at all.
check("a file with no active-region header IS still a fault",
      "parse fault" in fault)
check("the fault names the missing header rather than the entries",
      "active-region header" in fault)

# --- CONTROL: the message must DISCRIMINATE. A diagnostic that says the same thing
#     in both cases is decoration, not a signal.
check("fault and quiet-day messages differ", fault != quiet)

# --- The actual regression guard: main() must not return silently on zero.
cpq.PQ_FILE = pathlib.Path(tmp.name) / "quiet.md"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cpq.main()
printed = buf.getvalue().strip()
check(f"main() PRINTS on the zero path (got {printed[:60]!r})", bool(printed))
check("main()'s zero output carries the denominator", "section(s)" in printed)

# --- And it must still be a zero path: no notification was attempted. If main() had
#     fallen through to deliver(), it would have written a proactive file.
leaked = list(pathlib.Path(tmp.name).glob("proactive-*"))
check(f"no delivery attempted on the zero path (found {leaked})", not leaked)

passed = sum(ok for _, ok in CASES)
for name, ok in CASES:
    print(("  ok   " if ok else "  FAIL ") + name)
print(f"\n{passed}/{len(CASES)} passed")
tmp.cleanup()
sys.exit(0 if passed == len(CASES) else 1)
