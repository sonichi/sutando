#!/usr/bin/env python3
"""
Tests for `completion_line` in src/morning-briefing.py.

The script WRITES a result file; delivery is a channel bridge's job and may
never happen — no bridge running, or no channel configured on the host. The
old wording printed "Briefing delivered:" unconditionally, reporting an
outcome the script never observes: a run that reached nobody looked identical
to one that reached the owner.

Observed 2026-07-21 on a host with no channel configured: six proactive
results, oldest 8h old, sat undrained in results/ while every run printed
"delivered".

Covers:
  a) the line names the file that was actually written
  b) it carries the narrative through unchanged
  c) it does NOT claim the briefing was delivered
  d) the early-return (sentinel) path makes no delivery claim either

Run: python3 tests/morning-briefing-completion-line.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("mb", REPO / "src" / "morning-briefing.py")
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)


class _F:
    """Minimal stand-in for the result Path — only `.name` is used."""
    name = "proactive-morning-1784644049999.txt"


def case_a_names_the_file() -> list[str]:
    line = mb.completion_line(_F(), "Good morning.")
    if _F.name not in line:
        return [f"a) line should name the written file, got {line!r}"]
    return []


def case_b_carries_the_narrative() -> list[str]:
    line = mb.completion_line(_F(), "Good morning. It's 62F.")
    if "Good morning. It's 62F." not in line:
        return ["b) narrative must survive into the printed line"]
    return []


def case_c_makes_no_delivery_claim() -> list[str]:
    """The point of the change: never assert an outcome the script can't see."""
    line = mb.completion_line(_F(), "Good morning.")
    if "Briefing delivered" in line:
        return [f"c) line still claims delivery: {line!r}"]
    if "written" not in line.lower():
        return [f"c) line should say what actually happened (written), got {line!r}"]
    return []


def case_d_sentinel_path_makes_no_claim() -> list[str]:
    """The early return is a second branch that made the same claim — fixing
    only the one at the end of main() would have left half the bug in place."""
    src = (REPO / "src" / "morning-briefing.py").read_text()
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("print(") and "delivered" in s:
            return [f"d) a print still claims delivery: {s[:90]}"]
    return []



def _drive_main(*, sentinel_present: bool) -> str:
    """Run main() against stubbed collaborators and a temp workspace, returning
    stdout. Exercises the two print sites themselves — asserting on
    `completion_line` alone would leave main() free to print anything."""
    import io
    import tempfile
    import contextlib
    from datetime import datetime
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        results, state = td / "results", td / "state"
        results.mkdir(); state.mkdir()
        saved = {k: getattr(mb, k) for k in
                 ("RESULTS_DIR", "STATE_DIR", "get_weather", "get_calendar_events",
                  "get_reminders", "get_overnight_discord", "get_pending_questions",
                  "get_health_issues")}
        try:
            mb.RESULTS_DIR, mb.STATE_DIR = results, state
            mb.get_weather = lambda: "62F and overcast"
            mb.get_calendar_events = lambda: []
            mb.get_reminders = lambda: []
            mb.get_overnight_discord = lambda: []
            mb.get_pending_questions = lambda: []
            mb.get_health_issues = lambda: []
            if sentinel_present:
                today = datetime.now().strftime("%Y-%m-%d")
                (state / f"morning-briefing-{today}.sentinel").write_text("x")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mb.main()
            return buf.getvalue()
        finally:
            for k, v in saved.items():
                setattr(mb, k, v)


def case_e_main_prints_no_delivery_claim() -> list[str]:
    out = _drive_main(sentinel_present=False)
    if "Briefing delivered" in out:
        return ["e) main() still claims delivery on the write path"]
    if "written to" not in out:
        return [f"e) main() should report what it wrote, got: {out[-160:]!r}"]
    return []


def case_f_sentinel_return_prints_no_delivery_claim() -> list[str]:
    out = _drive_main(sentinel_present=True)
    if "delivered" in out:
        return [f"f) early return still claims delivery: {out.strip()[:110]!r}"]
    if "already generated" not in out:
        return [f"f) early return should say generated, got: {out.strip()[:110]!r}"]
    return []


def main() -> int:
    cases = [
        ("a", case_a_names_the_file),
        ("b", case_b_carries_the_narrative),
        ("c", case_c_makes_no_delivery_claim),
        ("d", case_d_sentinel_path_makes_no_claim),
        ("e", case_e_main_prints_no_delivery_claim),
        ("f", case_f_sentinel_return_prints_no_delivery_claim),
    ]
    failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nCompletion-line invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
