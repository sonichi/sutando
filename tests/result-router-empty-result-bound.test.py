#!/usr/bin/env python3
"""`empty_result_notice` — announce a stuck-empty result, never a mid-write one.

THE DEFECT. Both bridges skip an empty result file with a bare `continue`
(`discord-bridge.py:4308`, `telegram-bridge.py:970`) and `pending_replies.pop()`
happens AFTER it, so the task stays pending, re-read every 1s, until the 7-day
age-out at `discord-bridge.py:4261` logs `aged out N` without a reason. The owner
waits up to a week for a reply that never comes and nothing names it.

WHY THE OBVIOUS FIXES ARE BOTH WRONG, which is what this file has to encode:

  * DELETING the `continue` delivers an empty reply on a routine race. CLAUDE.md
    prescribes `cat > "<path>" << EOF`, and `>` truncates at open, so every
    normal result file is briefly present-and-empty. Measured on that exact
    write path: 1 of 8 observations caught it empty.
  * LOGGING ON FIRST SIGHT fires on nearly every delivery and is tuned out
    inside a day — the same silence in a louder font.

The only signal separating "empty for 2 ms" from "empty forever" is PERSISTENCE,
so the policy is a threshold, and the near-miss case below is the one that keeps
the fix honest: a file that fills up before the threshold must stay silent.

PURE: `result_router` does no I/O, so every policy case here is strings and ints.
The last case touches the filesystem on purpose — it demonstrates the truncate-at-
open state the threshold exists for, because a premise quoted from a docstring is
not evidence. It does so DETERMINISTICALLY rather than by racing a poller; see the
comment there for why the racing version had to go.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from result_router import (  # noqa: E402
    EMPTY_RESULT_POLL_THRESHOLD,
    empty_result_notice,
    note_empty_result,
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def main() -> int:
    print("empty-result bound:")
    T = EMPTY_RESULT_POLL_THRESHOLD
    P = "/w/results/task-1.txt"

    # --- THE NEAR-MISS: a mid-write file must stay SILENT ------------------
    # Every poll below the bound is indistinguishable from a partial write, so
    # every one of them must produce nothing.
    noisy = [n for n in range(1, T) if empty_result_notice("task-1", P, n) is not None]
    check("silent for every poll BELOW the threshold", not noisy,
          f"announced at {noisy} — would fire on normal partial writes")

    # --- fires, once, at the bound ----------------------------------------
    at = empty_result_notice("task-1", P, T)
    check("announces AT the threshold", at is not None, "stuck result stays silent")
    check("  ...names the task", at is not None and "task-1" in at, repr(at))
    check("  ...names the file", at is not None and P in at, repr(at))
    check("  ...says the reply is NOT being delivered",
          at is not None and "NOT being delivered" in at, repr(at))
    check("  ...names the 7-day age-out it pre-empts",
          at is not None and "7-day" in at, repr(at))

    # --- and NEVER again --------------------------------------------------
    # `== threshold`, not `>=`: a warning repeating every 1s for seven days is
    # the same silence in a louder font.
    repeats = [n for n in range(T + 1, T + 40) if empty_result_notice("task-1", P, n) is not None]
    check("NEVER repeats after the threshold", not repeats,
          f"re-announced at {repeats} — a 3s nag for 7 days")

    # --- the threshold is honoured, not hardcoded -------------------------
    check("a caller-supplied threshold is used",
          empty_result_notice("t", P, 3, threshold=3) is not None
          and empty_result_notice("t", P, 2, threshold=3) is None,
          "threshold arg ignored")

    # --- the bound must sit ABOVE any real write window -------------------
    # Demonstrated without a race: `>` truncates AT OPEN, so open-without-writing
    # reproduces the observable state the bridge sees, with no timing involved.
    box = pathlib.Path(tempfile.mkdtemp(prefix="empty-result-"))
    target = box / "r.txt"
    target.write_text("a previous, complete result")
    fh = open(target, "w")                      # truncate-at-open, nothing written yet
    try:
        mid_write_exists = target.exists()
        mid_write_size = target.stat().st_size
    finally:
        fh.write("the real body")
        fh.close()
    check("PREMISE: truncate-at-open leaves the file PRESENT and EMPTY",
          mid_write_exists and mid_write_size == 0,
          f"exists={mid_write_exists} size={mid_write_size} — if this ever fails, "
          f"investigate before touching the guard; do NOT read it as license to "
          f"delete the `continue`")
    check("  ...and the completed write is non-empty, so the skip is transient",
          target.stat().st_size > 0, f"size {target.stat().st_size}")

    # --- the COUNTER, which is policy and therefore shared -----------------
    # Counting lives in result_router, not in each bridge: two verbatim copies of a
    # five-line helper is still copied policy. One implementation, covered once.
    counters: dict = {}
    fired = [note_empty_result(counters, "t", P) for _ in range(T + 5)]
    announced = [f for f in fired if f is not None]
    check("the shared counter reaches the bound and announces once",
          counters["t"] == T + 5 and len(announced) == 1,
          f"counter={counters.get('t')} announced={len(announced)}")
    check("  ...and per-task, not global",
          note_empty_result(counters, "other-task", P) is None,
          "a second task inherited the first task's count")

    # --- WIRING: a policy with no caller is the defect it fixes ------------
    # A bound with no caller is the same latent no-op this change removes, so
    # assert the bridge actually delegates.
    for bridge in ("discord-bridge.py",):
        src = (REPO / "src" / bridge).read_text()
        # Pinned to the exact call spelling: an assertion on a contract exists to
        # notice when the contract moves, so re-point it rather than loosen it.
        check(f"{bridge} delegates counting to the shared policy",
              "result_router.note_empty_result(" in src,
              "does not delegate — the policy would be a no-op here, and two "
              "bridges would drift")
        check(f"  ...{bridge} does NOT re-implement the counting itself",
              "counters.get(task_id, 0) + 1" not in src
              and "_empty_result_polls.get(task_id, 0) + 1" not in src,
              "copied policy — CLAUDE.md: do not copy policy between bridges")
        check(f"  ...{bridge} CLEARS its counter on a successful delivery",
              "_empty_result_polls.pop(task_id, None)" in src,
              "a counter that never clears announces a healthy task after a "
              "few transient empty reads")
        # Emptiness lives in the shared read_ready_result owner, so assert that
        # spelling: the contract moved, and the assertion follows it rather than loosening.
        check(f"  ...{bridge} still SKIPS rather than delivering the empty body",
              "read_ready_result(result_file)" in src
              and "if reply_text is None:" in src and src.count("continue") > 0,
              "the partial-write guard was removed")

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All empty-result-bound checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
