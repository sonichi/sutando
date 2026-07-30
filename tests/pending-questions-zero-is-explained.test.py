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
