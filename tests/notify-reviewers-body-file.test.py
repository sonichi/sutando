#!/usr/bin/env python3
"""notify_reviewers accepts prose from a file, so it never crosses a shell boundary.

`--message` takes the body as argv. A backtick or an apostrophe in ordinary
prose is rewritten by the shell BEFORE this process starts, and the send still
reports success — the sender sees their draft, the reviewer sees something
shorter. Measured 2026-09-02: two spans were silently dropped from a review
request that had already gone out.

The repo already owns that policy in `src/body_file.py` (#2918, adopted by
bot2bot-post and discord-bridge). These arms pin that notify_reviewers uses the
SHARED reader rather than a fourth private spelling, and that the body arrives
byte-identical.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def run(*args):
    """Plan mode — no --send, so nothing is delivered by any arm here."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(TOOL), *args],
                          capture_output=True, text=True, cwd=str(REPO), env=env)


# The body that motivated this: a backtick pair and an apostrophe, which a
# double-quoted shell argument would execute and truncate rather than pass.
HAZARD = "re-review https://github.com/sonichi/sutando/pull/3303 — `merge-ready.py` says it's clean"

print("case: --body-file is accepted in place of --message")
# SCOPE: the read itself is tested by tests/body-file-read.test.py (#2918).
# These arms pin ADOPTION — that this CLI reaches the shared reader.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ask.md"
    p.write_text(HAZARD + "\n")
    r = run("--reviewers", "definitely-not-a-roster-key", "--body-file", str(p))
    out = r.stdout + r.stderr
    check("argparse did not demand --message", "required: --message" in out, False)
    check("--body-file is a known flag", "unrecognized arguments" in out, False)

print("\ncase: exactly one of --message / --body-file")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ask.md"
    p.write_text(HAZARD + "\n")
    r = run("--reviewers", "x", "--message", "hi", "--body-file", str(p))
    check("both -> refused", "give exactly one of" in (r.stdout + r.stderr), True)
    r = run("--reviewers", "x")
    check("neither -> refused", "give exactly one of" in (r.stdout + r.stderr), True)

print("\ncase: an empty body is refused rather than sent")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "empty.md"
    p.write_text("\n\n  \n")
    r = run("--reviewers", "x", "--body-file", str(p))
    check("empty -> refused", "refusing to send" in (r.stdout + r.stderr), True)

print("\ncase: it DELEGATES to src/body_file.py rather than re-implementing the read")
# A private copy would drift from the shared limit and the FIFO guard, which is
# the whole reason #2918 put this in one module.
src = TOOL.read_text()
check("imports the shared reader", "from body_file import read_body_file" in src, True)
check("no private open() of the body path", "open(a.body_file" in src, False)

print("\ncase: the shared reader's own refusals still apply")
with tempfile.TemporaryDirectory() as td:
    missing = Path(td) / "nope.md"
    r = run("--reviewers", "x", "--body-file", str(missing))
    check("a missing file is an ERROR, not an empty body",
          "cannot read --body-file" in (r.stdout + r.stderr), True)
    if hasattr(os, "mkfifo"):
        fifo = Path(td) / "fifo"
        os.mkfifo(fifo)
        r = run("--reviewers", "x", "--body-file", str(fifo))
        check("a FIFO is refused rather than blocking forever",
              "not a regular file" in (r.stdout + r.stderr), True)
    else:
        print("  note FIFO refusal is covered by the shared reader's POSIX suite")

print("\ncase: resolve_body, in-process")
# In-process as well as via the CLI: a subprocess run is invisible to coverage,
# so the arms above exercise these lines without ever attributing them.
import importlib.util
_spec = importlib.util.spec_from_file_location("nr", str(TOOL))
nr = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(nr)
except SystemExit:
    pass

check("argv body passes through unchanged", nr.resolve_body(HAZARD, None), HAZARD)

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "ask.md"
    p.write_text(HAZARD + "\n")
    check("a file body is read VERBATIM — backtick and apostrophe intact",
          nr.resolve_body(None, str(p)), HAZARD)

    for label, args in (("both", (HAZARD, str(p))), ("neither", (None, None))):
        try:
            nr.resolve_body(*args)
            check(f"{label} -> SystemExit", "no raise", "SystemExit")
        except SystemExit as e:
            check(f"{label} -> SystemExit naming the rule",
                  "give exactly one of" in str(e), True)

    empty = Path(td) / "empty.md"
    empty.write_text("\n  \n")
    try:
        nr.resolve_body(None, str(empty))
        check("empty file -> SystemExit", "no raise", "SystemExit")
    except SystemExit as e:
        check("empty file -> refuses rather than sending a blank ask",
              "refusing to send" in str(e), True)

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s)")
    sys.exit(1)
print("\nPASS — notify-reviewers --body-file")
