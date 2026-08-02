#!/usr/bin/env python3
"""The boot-abort question the auth gate writes must actually be COUNTED.

`src/auth-preflight-gate.sh` records a pending question when it stops a boot on a
logged-out CLI. It appended with `>>`.

Every reader of `pending-questions.md` — the notifier, morning-briefing, agent-api,
friction-detector, dashboard — counts only the text ABOVE the file's top-level
`# Resolved` divider; everything below is the audit trail. So an EOF append lands
BELOW the divider and is permanently uncounted. Measured against the real file on
this host (2099 lines, divider at line 1652):

    baseline                             21 waiting
    after the gate's `>>` EOF append     21   <- INVISIBLE
    same text placed above the divider   22   <- counted

The failure reports success in every cheap way a writer can check: the bytes land,
the path resolves, nothing errors, `wc -c` grows. Only calling the reader exposes
the zero — which is why this test drives `get_waiting_questions()` rather than
grepping the file.

And it is the worst possible entry to lose. The gate fires exactly when a boot was
ABORTED, so the durable record of *why the machine did not come up* is dropped at
the moment the owner most needs it. (The `results/proactive-*.txt` DM still goes
out — that path was never broken — so the symptom is a missing record, not silence.)

The fix inserts at the TOP of the active region rather than "just above the
divider", so it needs no divider regex and cannot be defeated by the divider
edge cases #2419 catalogues (a quoted `# Resolved` inside an HTML comment, a fenced
block, a wrapped inline code span). A boot-abort question also belongs first.

Run:  python3 tests/auth-gate-question-is-counted.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "src" / "auth-preflight-gate.sh"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail[:400]}")


def waiting(path: Path) -> int:
    """Count via the SHIPPED reader, not a local re-implementation."""
    spec = importlib.util.spec_from_file_location(
        "cpq", REPO / "src" / "check-pending-questions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PQ_FILE = path
    return len(m.get_waiting_questions())


#: A file shaped like a real one: an H1, some open questions, then the divider
#: with an audit trail under it.
FIXTURE = """# Pending Questions

## [2026-08-01 10:00Z] First open question
Body of the first.

## [2026-08-01 11:00Z] Second open question
Body of the second.

# Resolved

## [2026-07-30 09:00Z] An answered one
Answered on 2026-07-30.
"""


def extract_writer() -> str:
    """Pull the write block out of the gate and make it runnable standalone.

    The gate exits 2 and shells out to scutil/osascript, so it cannot be run
    directly. Extracting the block exercises the REAL lines rather than a
    paraphrase — a grep would pass against any rewrite, including one that
    still appends.
    """
    src = GATE.read_text()
    start = src.index('_pq="$_ws/hosts/$_host/pending-questions.md"')
    end = src.index('rm -f "$_pq.new"', start) + len('rm -f "$_pq.new"')
    return src[start:end]


print("auth-gate pending-question placement")

body = GATE.read_text()

# --- 0. control: the extraction found the real block ------------------------
try:
    block = extract_writer()
except ValueError as e:
    block = ""
    check("the write block was extracted from the real gate", False, str(e))
check("the write block was extracted from the real gate",
      "BOOT ABORTED" in block and "pending-questions.md" in block,
      f"got {len(block)} bytes")

# --- 1. the regression: the gate must not append at EOF ---------------------
check("the gate no longer appends the question with `>>`",
      ">> \"$_ws/hosts/$_host/pending-questions.md\"" not in body
      and '>> "$_pq"' not in body,
      "an EOF append lands below the divider and is never counted")

if block:
    with tempfile.TemporaryDirectory() as td:
        pq = Path(td) / "pending-questions.md"
        pq.write_text(FIXTURE)
        before = waiting(pq)

        env = dict(os.environ)
        env.update({"_ws": str(Path(td)), "_host": "TestHost",
                    "_ts": "2026-08-02T13:30:00Z",
                    "_remedy": "run `claude login`"})
        (Path(td) / "hosts" / "TestHost").mkdir(parents=True)
        (Path(td) / "hosts" / "TestHost" / "pending-questions.md").write_text(FIXTURE)
        r = subprocess.run(["bash", "-c", "set -e\n" + block],
                           capture_output=True, text=True, env=env, timeout=60)
        check("the extracted block runs cleanly", r.returncode == 0,
              f"rc={r.returncode} err={r.stderr[-300:]}")

        out = Path(td) / "hosts" / "TestHost" / "pending-questions.md"
        after = waiting(out)

        # THE assertion. `before` is the control: a fixture that already counts
        # non-zero proves the reader works, so a +1 is meaningful.
        check("control: the fixture's open questions are counted to begin with",
              before == 2, f"got {before}, expected 2")
        check("the gate's question is COUNTED by the shipped reader",
              after == before + 1,
              f"{before} -> {after}; unchanged means it landed below the divider")

        text = out.read_text()
        # --- 2. it went ABOVE the divider, not merely somewhere -------------
        div = re.search(r'^#[ \t]+Resolved\b', text, re.M)
        boot = text.find("BOOT ABORTED")
        check("...and physically sits above the `# Resolved` divider",
              div is not None and 0 <= boot < div.start(),
              f"boot at {boot}, divider at {div.start() if div else None}")

        # --- 3. nothing was lost ---------------------------------------------
        check("the pre-existing content is preserved verbatim",
              "First open question" in text and "Second open question" in text
              and "An answered one" in text, text[:200])
        check("the H1 is still the first line",
              text.splitlines()[0] == "# Pending Questions",
              repr(text.splitlines()[0]))

        # --- 4. a file with NO divider still works ---------------------------
        pq2 = Path(td) / "hosts" / "TestHost" / "pending-questions.md"
        pq2.write_text("# Pending Questions\n\n## Only question\nBody.\n")
        n_before = waiting(pq2)
        r2 = subprocess.run(["bash", "-c", "set -e\n" + block],
                            capture_output=True, text=True, env=env, timeout=60)
        check("a file with no divider still gets the question counted",
              r2.returncode == 0 and waiting(pq2) == n_before + 1,
              f"rc={r2.returncode}, {n_before} -> {waiting(pq2)}")

        # --- 5. a MISSING file is created, not skipped -----------------------
        pq2.unlink()
        r3 = subprocess.run(["bash", "-c", "set -e\n" + block],
                            capture_output=True, text=True, env=env, timeout=60)
        check("a missing file is created with the question counted",
              r3.returncode == 0 and pq2.exists() and waiting(pq2) == 1,
              f"rc={r3.returncode} exists={pq2.exists()} "
              f"n={waiting(pq2) if pq2.exists() else 'n/a'}")

        # --- 6. no scratch files left behind ---------------------------------
        leftovers = sorted(p.name for p in pq2.parent.glob("*.new")) + \
                    sorted(p.name for p in pq2.parent.glob("*.tmp"))
        check("no .new/.tmp scratch files are left behind", not leftovers,
              str(leftovers))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — the boot-abort question is written where readers count it")
