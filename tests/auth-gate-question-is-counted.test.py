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
import shutil
import subprocess
import sys
import tempfile
import time
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
    # Anchor on a sentinel that occurs ONCE, never on a line of the block's own
    # logic. This used to anchor on `rmdir "$_lock"`, which also appeared in the
    # stale-lock breaker inside the spin loop; the extraction truncated mid-loop
    # and produced a bash syntax error that read as a gate bug. That breaker is
    # gone now (see the ABA note in the gate), so `rmdir` is once again unique —
    # which is exactly why the anchor must NOT move back to it. An anchor whose
    # uniqueness depends on the current shape of the code re-breaks the next
    # time the code changes.
    end = src.index("# --- end pending-question write", start)
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

        # --- 7. CONCURRENT gates must not lose an entry ----------------------
        # Found in review at f4e17019: fixed sibling scratch names (`$_pq.new`,
        # `$_pq.tmp`) let two gates for the same host clobber each other —
        # measured rcs [1, 0] with the reader seeing 2 of 3. That regresses the
        # durability of the `>>` path this replaces, in the one record meant to
        # explain why startup aborted. Visibility is not worth losing an entry.
        import concurrent.futures as _cf

        pq2.write_text(FIXTURE)
        base = waiting(pq2)

        def _fire(n: int):
            e = dict(env)
            e["_ts"] = f"2026-08-02T13:4{n}:00Z"
            e["_remedy"] = f"remedy-{n}"
            return subprocess.run(["bash", "-c", "set -e\n" + block],
                                  capture_output=True, text=True, env=e, timeout=120)

        N = 5
        with _cf.ThreadPoolExecutor(max_workers=N) as pool:
            results = list(pool.map(_fire, range(N)))

        check("every concurrent gate exits 0",
              all(r.returncode == 0 for r in results),
              str([r.returncode for r in results]))
        final = pq2.read_text()
        present = [n for n in range(N) if f"remedy-{n}" in final]
        check(f"all {N} concurrent boot-abort records survive",
              len(present) == N,
              f"only {present} present — a lost record is the exact regression under review")
        check("...and the reader counts every one of them",
              waiting(pq2) == base + N,
              f"{base} -> {waiting(pq2)}, expected {base + N}")
        # `.find()`, not `.index()`: a MISSING record must be reported by the
        # check above, not raise here and abort the suite. The first CI run of
        # this file crashed exactly that way — a lost record surfaced as a
        # traceback rather than a FAIL line, which hides which assertion bit.
        _div = re.search(r'^#[ \t]+Resolved\b', final, re.M)
        _pos = [final.find(f"remedy-{n}") for n in range(N)]
        check("...and they all sit above the divider",
              _div is None or all(0 <= q < _div.start() for q in _pos),
              f"positions {_pos}, divider at {_div.start() if _div else None} "
              f"(-1 = that record is absent, see the check above)")
        strays = sorted(p.name for p in pq2.parent.iterdir()
                        if p.name.startswith("pending-questions.md.")
                        and p.name != "pending-questions.md")
        check("no per-invocation scratch or lock left behind", not strays, str(strays))

        # --- 8. a HELD, FRESH foreign lock must FAIL CLOSED ------------------
        # Review at 72833b61: the bounded wait used to `break` and proceed
        # WITHOUT the lock, then unconditionally `rmdir` it. Reproduced there as
        # 10 writers, all returning 0, 2 of 10 records surviving, and the
        # foreign lock deleted. The escape hatch re-entered the exact silent
        # record-loss this change closes. Fail closed instead: touch neither the
        # file nor a lock we do not own.
        pq2.write_text(FIXTURE)
        held = Path(str(pq2) + ".lock")
        held.mkdir()
        before_bytes = pq2.read_text()
        n_before2 = waiting(pq2)
        e = dict(env); e["_ts"] = "2026-08-02T14:00:00Z"; e["_remedy"] = "remedy-locked"
        r4 = subprocess.run(["bash", "-c", "set -e\n" + block],
                            capture_output=True, text=True, env=e, timeout=180)
        check("a held foreign lock does NOT crash the gate", r4.returncode == 0,
              f"rc={r4.returncode} err={r4.stderr[-200:]}")
        check("...and the file is left byte-identical", pq2.read_text() == before_bytes,
              "the writer mutated the file without owning the lock")
        check("...and the count is unchanged", waiting(pq2) == n_before2,
              f"{n_before2} -> {waiting(pq2)}")
        check("...and the FOREIGN lock still exists", held.is_dir(),
              "the writer removed a lock it never acquired")
        check("...and it says so on stderr rather than failing silently",
              "could not acquire" in r4.stderr, repr(r4.stderr[-200:]))
        # Teardown, not an assertion — every claim about the foreign lock is
        # made above and is unchanged. Guarded because a bare `rmdir` here
        # raised FileNotFoundError once in a clean run at this head, after all
        # five checks had PASSED. Three subsequent runs could not reproduce it,
        # so this is hardening, NOT a diagnosis — I am not claiming to know why
        # the directory was gone. A teardown that asserts state it never
        # re-reads is the wrong place to learn that, and an intermittent red
        # here would look like the fail-closed behaviour regressing when it did
        # not.
        shutil.rmtree(held, ignore_errors=True)

        # Control: with the lock released, the same invocation DOES write —
        # so the four assertions above are about the lock, not about the fixture.
        r5 = subprocess.run(["bash", "-c", "set -e\n" + block],
                            capture_output=True, text=True, env=e, timeout=120)
        check("control: with the lock free, the same call writes normally",
              r5.returncode == 0 and waiting(pq2) == n_before2 + 1,
              f"rc={r5.returncode}, {n_before2} -> {waiting(pq2)}")

        # --- 9. ABA: a lock the gate did not acquire must SURVIVE, however old --
        # The prior head reclaimed a lock whose mtime passed 30s by calling
        # `rmdir "$_lock"`. `rmdir` names a PATH, not the directory that was
        # stat-ed: if the original owner releases and a fresh gate acquires in
        # the window between the two syscalls, the reclaimer deletes the
        # REPLACEMENT's lock, a third writer then acquires, and two writers sit
        # in the read-modify-write together — losing the boot-abort record.
        #
        # This asserts the property that makes the race impossible rather than
        # trying to hit the window: the gate removes NO lock it did not create.
        # Backdating well past the old 30s threshold is what makes it
        # discriminating — on the prior head the gate reclaims this lock, writes,
        # and `stale.is_dir()` is False. Verified by reverting the production
        # hunk: this check FAILS there and passes here.
        pq2.write_text(FIXTURE)
        stale = Path(str(pq2) + ".lock")
        stale.mkdir()
        old = time.time() - 3600            # 60 min: 120x the retired threshold
        os.utime(stale, (old, old))
        n_before3 = waiting(pq2)
        bytes_before3 = pq2.read_text()
        e2 = dict(env)
        e2["_ts"] = "2026-08-02T19:00:00Z"
        e2["_remedy"] = "remedy-aba"
        r6 = subprocess.run(["bash", "-c", "set -e\n" + block],
                            capture_output=True, text=True, env=e2, timeout=180)
        check("an HOUR-old foreign lock is still not reclaimed", stale.is_dir(),
              "the gate removed a lock it never acquired — a replacement "
              "acquired in the stat->rmdir window would have been destroyed")
        check("...and the gate still fails closed rather than writing",
              waiting(pq2) == n_before3 and pq2.read_text() == bytes_before3,
              f"{n_before3} -> {waiting(pq2)}; the file must be byte-identical")
        check("...and it exits 0 and says why", r6.returncode == 0
              and "could not acquire" in r6.stderr,
              f"rc={r6.returncode} err={r6.stderr[-200:]}")
        check("...and the stderr names the manual remedy, so a genuinely wedged "
              "lock is recoverable without reintroducing the race",
              "rmdir" in r6.stderr, repr(r6.stderr[-240:]))
        # Control: the age is what the previous head keyed on, so prove the age
        # was really applied — otherwise a no-op `utime` would make the four
        # checks above pass for the wrong reason.
        #
        # `os.stat` is guarded rather than called directly. On the pre-fix gate
        # this lock is GONE by now, and a bare stat raises FileNotFoundError,
        # which aborts the suite mid-run and hides which assertion bit — the
        # same failure mode section 7 above already had to fix. A control must
        # report FAIL, never crash; when the directory is missing the four
        # checks above have already said so.
        try:
            _age = time.time() - os.stat(stale).st_mtime
        except FileNotFoundError:
            _age = None
        check("control: the lock really is older than the retired 30s threshold",
              _age is not None and _age > 30,
              "the lock was removed before the control could read it (see the "
              "reclamation checks above)" if _age is None
              else f"mtime age {_age:.0f}s")
        shutil.rmtree(stale, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — the boot-abort question is written where readers count it")
